//! Everything specific to the title: its container formats, build
//! identities, and the hooks we place in its code (located by per-build
//! offsets or byte-pattern signatures). What may and may not be stored here
//! is defined in docs/asset-policy.md.

mod cityhash;
pub mod pipelines;
pub mod sgd5;
pub mod shaderpack;

/// A specific build of the title executable we know how to run.
#[derive(Debug, Clone, Copy)]
pub struct KnownBuild {
    /// Our own label, e.g. "1.0.0 retail".
    pub label: &'static str,
    /// BLAKE3 of the original executable, hex.
    pub blake3: &'static str,
}

/// Builds we support. Empty until the executable is available.
pub const KNOWN_BUILDS: &[KnownBuild] = &[];

/// Looks up a build by the BLAKE3 hash of its executable.
pub fn identify(blake3_hex: &str) -> Option<&'static KnownBuild> {
    KNOWN_BUILDS
        .iter()
        .find(|b| b.blake3.eq_ignore_ascii_case(blake3_hex))
}

#[cfg(test)]
fn compress_fixture(payload: &[u8], window_bits: i32) -> Vec<u8> {
    let mut buf = vec![0u8; zlib_rs::compress_bound(payload.len())];
    let config = zlib_rs::DeflateConfig {
        window_bits,
        ..zlib_rs::DeflateConfig::new(6)
    };
    let (packed, status) = zlib_rs::compress_slice(&mut buf, payload, config);
    assert_eq!(status, zlib_rs::ReturnCode::Ok);
    packed.to_vec()
}
