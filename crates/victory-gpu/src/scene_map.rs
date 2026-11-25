//! Opt-in observations consumed by the scene texture mapper.

use std::sync::OnceLock;

/// Whether this process has a scene map collector.
pub fn enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| std::env::var_os("VICTORY_SCENE_MAP").is_some_and(|v| v == "1"))
}

/// An open archive and the last frame in which it supplied a scene cue.
pub struct Archive {
    id: u64,
    last_region_frame: std::sync::atomic::AtomicU64,
}

/// Identifies the logical package file and its revision without persisting a host path.
pub fn archive(name: &str, file: &std::fs::File) -> Option<Archive> {
    if !enabled() {
        return None;
    }
    let mut hash = blake3::Hasher::new();
    let normalized = name.to_lowercase().replace('\\', "/");
    let package_path = normalized.strip_prefix("g:/").and_then(|relative| {
        let parts: Vec<_> = relative.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
        (!parts.is_empty() && parts.iter().all(|p| *p != ".." && !p.contains([':', '\n', '\r'])))
            .then(|| parts.join("/"))
    });
    hash.update(package_path.as_deref().unwrap_or(&normalized).as_bytes());
    if let Ok(metadata) = file.metadata() {
        hash.update(&metadata.len().to_le_bytes());
        if let Ok(time) = metadata.modified()
            && let Ok(age) = time.duration_since(std::time::UNIX_EPOCH)
        {
            hash.update(&age.as_nanos().to_le_bytes());
        }
    }
    let id = u64::from_le_bytes(hash.finalize().as_bytes()[..8].try_into().unwrap());
    if let Some(path) = package_path {
        tracing::info!(target: "victory_scene_map", "sm archive 0 {id:016x} {path}");
    }
    Some(Archive { id, last_region_frame: std::sync::atomic::AtomicU64::new(u64::MAX) })
}

/// Samples one non-texture extent per archive per presented frame.
pub fn region(file: &Archive, offset: u64) {
    let frame = crate::queue::presented();
    if file.last_region_frame.load(std::sync::atomic::Ordering::Relaxed) != frame
        && file.last_region_frame.swap(frame, std::sync::atomic::Ordering::Relaxed) != frame
    {
        let archive = file.id;
        let region = offset >> 22;
        tracing::info!(target: "victory_scene_map", "sm region {frame} {archive:016x} {region}");
    }
}

/// Records an archive extent with a content signature comparable to the uploaded guest texture.
pub fn texture(file: &Archive, offset: u64, compressed: u32, decoded: u32, transform: u64, intermediate: u32, bytes: &[u8]) {
    let archive = file.id;
    let frame = crate::queue::presented();
    let fingerprint = content_key(bytes);
    let n = bytes.len();
    tracing::info!(target: "victory_scene_map", "sm texture {frame} {archive:016x} {offset} {compressed} {decoded} {transform} {intermediate} {fingerprint:016x} {n}");
}

fn content_key(bytes: &[u8]) -> u64 {
    const WINDOW: usize = 16 << 10;
    let n = bytes.len();
    let mut hasher = blake3::Hasher::new();
    if n <= 3 * WINDOW {
        hasher.update(bytes);
    } else {
        let middle = (n / 2) & !0xfff;
        hasher.update(&bytes[..WINDOW]);
        hasher.update(&bytes[middle..middle + WINDOW]);
        hasher.update(&bytes[n - WINDOW..]);
    }
    u64::from_le_bytes(hasher.finalize().as_bytes()[..8].try_into().unwrap()) | 1
}

/// Records an indexed upload whose content fingerprint the renderer already computed.
pub(crate) fn upload(slot: u32, fingerprint: u64, size: u64) {
    let frame = crate::queue::presented();
    tracing::info!(target: "victory_scene_map", "sm upload {frame} {slot} {fingerprint:016x} {size}");
}

#[cfg(feature = "diagnostics")]
pub(crate) fn visual_frame(n: u64) -> bool {
    static VISUAL: OnceLock<bool> = OnceLock::new();
    enabled() && *VISUAL.get_or_init(|| std::env::var_os("VICTORY_SCENE_VISUAL").is_some_and(|v| v == "1"))
        && n.is_multiple_of(8)
}

/// Samples the presented game image. Flat frames clear the last visual cue.
#[cfg(feature = "diagnostics")]
pub(crate) fn picture(frame: u64, w: usize, h: usize, pitch: usize, bgra: bool, rows: &[u8]) {
    let mut bits = 0u64;
    let mut rgb = [0u32; 3];
    let (mut low, mut high) = (255u32, 0u32);
    for y in 0..8 {
        let mut prev = 0;
        for x in 0..9 {
            let i = ((2 * y + 1) * h / 16) * pitch + ((2 * x + 1) * w / 18) * 4;
            let pixel = &rows[i..i + 3];
            let [r, g, b] = [pixel[if bgra { 2 } else { 0 }], pixel[1], pixel[if bgra { 0 } else { 2 }]].map(u32::from);
            let value = (r * 3 + g * 6 + b) / 10;
            rgb[0] += r;
            rgb[1] += g;
            rgb[2] += b;
            low = low.min(value);
            high = high.max(value);
            if x > 0 {
                bits = (bits << 1) | u64::from(value > prev);
            }
            prev = value;
        }
    }
    let [r, g, b] = rgb.map(|v| v / 72);
    tracing::info!(target: "victory_scene_map", "sm visual {frame} {bits:016x} {r} {g} {b} {}", high - low);
}


#[cfg(test)]
mod tests {
    #[test]
    fn decoded_keys_match_uploaded_guest_memory() {
        let bytes: Vec<u8> = (0usize..200_000).map(|i| (i ^ (i >> 8)) as u8).collect();
        for n in [1usize, 49152, 49153, 65536, bytes.len()] {
            assert_eq!(super::content_key(&bytes[..n]), crate::texture::fingerprint(bytes.as_ptr() as u64, n as u64));
        }
    }
}
