//! Offline texture preparation calls the same BCPack decoder as DirectStorage.
use std::sync::OnceLock;
use victory_formats::bcpack::{self, Tables};

/// Decodes one complete BCPack request with VIctory's built-in tables.
///
/// # Safety
/// The caller supplies disjoint memory ranges of the declared lengths.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn victory_bcpack_decode(
    format: u8,
    source: *const u8,
    source_len: usize,
    target: *mut u8,
    target_len: usize,
) -> bool {
    static TABLES: OnceLock<Option<Tables>> = OnceLock::new();
    let Some(tables) = TABLES.get_or_init(|| Tables::builtin().ok()) else {
        return false;
    };
    if source.is_null()
        || target.is_null()
        || source_len > isize::MAX as usize
        || target_len > isize::MAX as usize
    {
        return false;
    }
    // SAFETY: the caller owns both ranges for this call.
    let (input, output) = unsafe {
        (
            std::slice::from_raw_parts(source, source_len),
            std::slice::from_raw_parts_mut(target, target_len),
        )
    };
    bcpack::decode(format, input, output, tables).is_ok()
}
