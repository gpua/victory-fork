//! Verified offline texture stores for file-backed DirectStorage requests.
use std::fs::File;
use std::io;
use std::os::windows::fs::{FileExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering};

const HEADER: u64 = 48;
const RECORD: usize = 48;
const MAX_BLOCK: usize = 4 << 20;
static HITS: [AtomicU64; 8] = [const { AtomicU64::new(0) }; 8];

struct Entry {
    offset: u64,
    source: u32,
    size: u32,
    options: u32,
    packed: u32,
    data: u64,
    hash: [u8; 16],
}

pub(super) struct Store {
    file: File,
    entries: Vec<Entry>,
}

fn read_at(file: &File, mut data: &mut [u8], mut offset: u64) -> io::Result<()> {
    while !data.is_empty() {
        let n = file.seek_read(data, offset)?;
        if n == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        offset += n as u64;
        data = &mut data[n..];
    }
    Ok(())
}

fn u64_at(bytes: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(bytes[at..at + 8].try_into().unwrap())
}

fn u32_at(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap())
}

impl Store {
    pub(super) fn open(name: &str, source: &File) -> Option<Self> {
        static ROOT: OnceLock<Option<PathBuf>> = OnceLock::new();
        let root = ROOT
            .get_or_init(|| std::env::var_os("VICTORY_TEXTURE_PACKS").map(PathBuf::from))
            .as_ref()?;
        let name = Path::new(name).file_name()?.to_str()?.to_ascii_lowercase();
        if !name.ends_with(".ttpf") && name != "textures.rpf" {
            return None;
        }
        let meta = source.metadata().ok()?;
        let path = root.join(format!(
            "{name}.{:x}.{:x}.vtxz",
            meta.file_size(),
            meta.last_write_time()
        ));
        let file = File::open(path).ok()?;
        let result = Self::load(file, source, meta.file_size());
        if result.is_none() {
            tracing::warn!(%name, "offline texture index rejected");
        }
        result
    }

    fn load(file: File, source: &File, source_size: u64) -> Option<Self> {
        let mut header = [0u8; HEADER as usize];
        read_at(&file, &mut header, 0).ok()?;
        let count = u64_at(&header, 8);
        let index = u64_at(&header, 16);
        if &header[..8] != b"VTXZ0001"
            || u64_at(&header, 40) != source_size
            || count > 4_000_000
            || index < HEADER
            || index.checked_add(count * RECORD as u64)? != file.metadata().ok()?.len()
        {
            return None;
        }
        let mut prefix = vec![0; source_size.min(4096) as usize];
        read_at(source, &mut prefix, 0).ok()?;
        if blake3::hash(&prefix).as_bytes()[..16] != header[24..40] {
            return None;
        }
        let mut table = vec![0; count as usize * RECORD];
        read_at(&file, &mut table, index).ok()?;
        let mut entries = Vec::with_capacity(count as usize);
        let mut last_end = 0;
        for row in table.chunks_exact(RECORD) {
            let e = Entry {
                offset: u64_at(row, 0),
                source: u32_at(row, 8),
                size: u32_at(row, 12),
                options: u32_at(row, 16),
                packed: u32_at(row, 20),
                data: u64_at(row, 24),
                hash: row[32..48].try_into().ok()?,
            };
            if e.offset < last_end
                || e.source == 0
                || e.size == 0
                || e.size as usize > MAX_BLOCK
                || e.packed == 0
                || e.packed as usize > MAX_BLOCK * 2
                || e.data < HEADER
                || e.data.checked_add(u64::from(e.packed))? > index
            {
                return None;
            }
            last_end = e.offset.checked_add(u64::from(e.source))?;
            if last_end > source_size {
                return None;
            }
            entries.push(e);
        }
        Some(Self { file, entries })
    }

    pub(super) fn decode(
        &self,
        offset: u64,
        source: u32,
        size: u32,
        options: u32,
    ) -> Option<Vec<u8>> {
        let e = &self.entries[self
            .entries
            .binary_search_by_key(&offset, |e| e.offset)
            .ok()?];
        if (e.source, e.size, e.options) != (source, size, options) {
            return None;
        }
        let mut packed = vec![0; e.packed as usize];
        read_at(&self.file, &mut packed, e.data).ok()?;
        let raw = zstd::bulk::decompress(&packed, size as usize).ok()?;
        if raw.len() != size as usize || blake3::hash(&raw).as_bytes()[..16] != e.hash {
            return None;
        }
        let codec = ((options >> 1) & 7) as usize;
        let hit = HITS[codec].fetch_add(1, Ordering::Relaxed) + 1;
        if hit.is_power_of_two() {
            tracing::info!(hit, size, options, "offline texture request served");
        }
        Some(raw)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn serves_only_verified_exact_requests() {
        let mut source = tempfile::tempfile().unwrap();
        let source_data = vec![7u8; 8192];
        source.write_all(&source_data).unwrap();
        let raw = [42u8; 4096];
        let packed = zstd::bulk::compress(&raw, 1).unwrap();
        let index = HEADER + packed.len() as u64;
        let mut data = Vec::from(*b"VTXZ0001");
        data.extend_from_slice(&1u64.to_le_bytes());
        data.extend_from_slice(&index.to_le_bytes());
        data.extend_from_slice(&blake3::hash(&source_data[..4096]).as_bytes()[..16]);
        data.extend_from_slice(&(source_data.len() as u64).to_le_bytes());
        data.extend_from_slice(&packed);
        data.extend_from_slice(&256u64.to_le_bytes());
        for field in [32u32, 4096, 14, packed.len() as u32] {
            data.extend_from_slice(&field.to_le_bytes());
        }
        data.extend_from_slice(&HEADER.to_le_bytes());
        data.extend_from_slice(&blake3::hash(&raw).as_bytes()[..16]);
        let open = |bytes: &[u8]| {
            let mut file = tempfile::tempfile().unwrap();
            file.write_all(bytes).unwrap();
            Store::load(file, &source, 8192)
        };
        let store = open(&data).unwrap();
        assert_eq!(store.decode(256, 32, 4096, 14).unwrap(), raw);
        assert!(store.decode(256, 32, 4096, 1).is_none());
        assert!(store.decode(256, 31, 4096, 14).is_none());
        assert!(store.decode(256, 32, 16, 14).is_none());
        assert!(store.decode(272, 32, 4096, 14).is_none());
        *data.last_mut().unwrap() ^= 1;
        assert!(open(&data).unwrap().decode(256, 32, 4096, 14).is_none());
        data[HEADER as usize] ^= 255;
        assert!(open(&data).unwrap().decode(256, 32, 4096, 14).is_none());
        data[24] ^= 1;
        assert!(open(&data).is_none());
        assert!(open(&data[..data.len() - 1]).is_none());
    }
}
