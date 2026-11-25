//! Persistently mapped upload regions for texture staging.

use crate::host::Host;
use std::sync::{Mutex, OnceLock};
use windows::Win32::Graphics::Direct3D12::{
    ID3D12Resource, D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT,
};

const ALIGN: u64 = D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT as u64;
const CLASSES: usize = 11; // 512 bytes through 512 KiB.
const REGIONS: usize = 8;

#[derive(Clone)]
pub(super) struct Region {
    pub buffer: ID3D12Resource,
    pub offset: u64,
    pub data: *mut u8,
}

// SAFETY: each checked-out region has one staging owner. The free list
// transfers ownership under its mutex without accessing the mapped bytes.
unsafe impl Send for Region {}

static FREE: Mutex<[Vec<Region>; CLASSES]> = Mutex::new([const { Vec::new() }; CLASSES]);

pub(super) struct Upload {
    pub region: Region,
    class: Option<usize>,
}

fn size_class(bytes: u64) -> Option<(usize, u64)> {
    let size = bytes.max(ALIGN).checked_next_power_of_two()?;
    let class = (size / ALIGN).ilog2() as usize;
    (class < CLASSES).then_some((class, size))
}

impl Upload {
    pub fn new(host: &Host, bytes: u64) -> Option<Self> {
        static POOL: OnceLock<bool> = OnceLock::new();
        if *POOL.get_or_init(|| std::env::var_os("VICTORY_UPLOAD_POOL").is_none_or(|v| v != "0"))
            && let Some((class, stride)) = size_class(bytes)
        {
            let spare = FREE.lock().unwrap_or_else(|e| e.into_inner())[class].pop();
            if let Some(region) = spare {
                // A first upload can see unmapped guest pages. Preserve the
                // zero-initialized contents of a dedicated upload resource.
                // SAFETY: this lease owns a slot at least as large as bytes.
                unsafe { region.data.write_bytes(0, bytes as usize) };
                return Some(Self {
                    region,
                    class: Some(class),
                });
            }
            if let Some((buffer, base)) = host.upload_buffer(stride * REGIONS as u64) {
                crate::host::name(&buffer, || {
                    format!("texture upload pool, {stride} byte slots")
                });
                let mut free = FREE.lock().unwrap_or_else(|e| e.into_inner());
                for i in 1..REGIONS {
                    let offset = i as u64 * stride;
                    if free[class].len() >= REGIONS {
                        break;
                    }
                    // SAFETY: the mapped buffer holds REGIONS disjoint slots.
                    let data = unsafe { base.add(offset as usize) };
                    free[class].push(Region {
                        buffer: buffer.clone(),
                        offset,
                        data,
                    });
                }
                return Some(Self {
                    region: Region {
                        buffer,
                        offset: 0,
                        data: base,
                    },
                    class: Some(class),
                });
            }
        }
        let (buffer, data) = host.upload_buffer(bytes)?;
        crate::host::name(&buffer, || {
            format!("dedicated texture upload, {bytes} bytes")
        });
        Some(Self {
            region: Region {
                buffer,
                offset: 0,
                data,
            },
            class: None,
        })
    }
}

impl Drop for Upload {
    fn drop(&mut self) {
        // Recorded lists retain the staging Arc through fence completion.
        // Only its final owner returns the region to this bounded cache.
        if let Some(class) = self.class {
            let mut free = FREE.lock().unwrap_or_else(|e| e.into_inner());
            if free[class].len() < REGIONS {
                free[class].push(self.region.clone());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_fit_their_aligned_regions() {
        assert_eq!(size_class(1), Some((0, ALIGN)));
        assert_eq!(size_class(ALIGN + 1), Some((1, 2 * ALIGN)));
        assert_eq!(size_class(512 << 10), Some((10, 512 << 10)));
        assert_eq!(size_class((512 << 10) + 1), None);
        assert_eq!(size_class(u64::MAX), None);
    }

    #[test]
    fn live_upload_regions_are_disjoint_and_return_clean() {
        let Some(host) = crate::host::host() else {
            return;
        };
        let a = Upload::new(host, 777).unwrap();
        let b = Upload::new(host, 777).unwrap();
        let readback = host.read_back_buffer(2).unwrap();
        let list = host.new_list().unwrap();
        // SAFETY: both leases own 777 mapped bytes and remain alive.
        unsafe {
            assert_ne!(a.region.data, b.region.data);
            a.region.data.write_bytes(0x3a, 777);
            b.region.data.write_bytes(0xd1, 777);
            assert_eq!(*a.region.data.add(776), 0x3a);
            list.list
                .CopyBufferRegion(&readback, 0, &a.region.buffer, a.region.offset + 776, 1);
            list.list
                .CopyBufferRegion(&readback, 1, &b.region.buffer, b.region.offset + 776, 1);
            list.close();
            host.execute(&[&list]);
            let mut data = std::ptr::null_mut();
            readback.Map(0, None, Some(&mut data)).unwrap();
            assert_eq!(
                std::slice::from_raw_parts(data.cast::<u8>(), 2),
                [0x3a, 0xd1]
            );
            readback.Unmap(0, None);
            drop(b);
            let c = Upload::new(host, 777).unwrap();
            assert!(std::slice::from_raw_parts(c.region.data, 777)
                .iter()
                .all(|&v| v == 0));
            assert_eq!(*a.region.data.add(776), 0x3a);
        }
    }
}
