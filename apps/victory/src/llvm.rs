//! Offline LLVM optimization of translated DirectX shaders.
use std::ffi::c_void;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, ensure};
use victory_gpu::shader_cache::{OptimizedShaders, cached_dxil};
use victory_shader::{Container, DxilProgram, FourCC};
use windows::Win32::Foundation::{FreeLibrary, HMODULE};
use windows::Win32::Graphics::Direct3D::Dxc::*;
use windows::Win32::System::LibraryLoader::{
    GetProcAddress, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR, LOAD_LIBRARY_SEARCH_SYSTEM32, LoadLibraryExW,
};
use windows::core::{GUID, HRESULT, HSTRING, Interface, s, w};

struct Library(HMODULE);

impl Library {
    fn load(path: &Path) -> Result<Self> {
        let path = path
            .canonicalize()
            .with_context(|| path.display().to_string())?;
        // SAFETY: the absolute path lives through the call. Only the SDK and system directories
        // are searched for dependencies.
        Ok(Self(unsafe {
            LoadLibraryExW(
                &HSTRING::from(path.as_os_str()),
                None,
                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32,
            )?
        }))
    }

    fn create<T: Interface>(&self, class: &GUID) -> Result<T> {
        type Create =
            unsafe extern "system" fn(*const GUID, *const GUID, *mut *mut c_void) -> HRESULT;
        // SAFETY: both SDK libraries export DxcCreateInstance with this ABI. Success transfers
        // one reference to the requested interface. The caller retains the library.
        unsafe {
            let proc = GetProcAddress(self.0, s!("DxcCreateInstance"))
                .context("missing DxcCreateInstance")?;
            let create: Create = std::mem::transmute(proc);
            let mut raw = std::ptr::null_mut();
            create(class, &T::IID, &mut raw).ok()?;
            ensure!(!raw.is_null(), "SDK returned a null interface");
            Ok(T::from_raw(raw))
        }
    }
}

impl Drop for Library {
    fn drop(&mut self) {
        // SAFETY: this handle owns one loader reference.
        unsafe {
            let _ = FreeLibrary(self.0);
        }
    }
}

struct Optimizer {
    utils: IDxcUtils,
    opt: IDxcOptimizer,
    assembler: IDxcAssembler,
    validator: IDxcValidator,
    // Fields drop in declaration order. Release interfaces before their DLLs.
    _compiler: Library,
    _signer: Library,
}

impl Optimizer {
    fn open(path: &Path) -> Result<Self> {
        let signer = Library::load(&path.join("dxil.dll"))?;
        let compiler = Library::load(&path.join("dxcompiler.dll"))?;
        Ok(Self {
            utils: compiler.create(&CLSID_DxcUtils)?,
            opt: compiler.create(&CLSID_DxcOptimizer)?,
            assembler: compiler.create(&CLSID_DxcAssembler)?,
            validator: signer.create(&CLSID_DxcValidator)?,
            _compiler: compiler,
            _signer: signer,
        })
    }

    fn optimize(&self, input: &[u8]) -> Result<Vec<u8>> {
        let container = Container::parse(input)?;
        // The assembler derives interface metadata from LLVM. It cannot preserve arbitrary
        // external chunks, such as an embedded root signature or debug information.
        ensure!(
            container.parts.iter().all(|p| matches!(
                &p.fourcc.0,
                b"DXIL" | b"HASH" | b"ISG1" | b"OSG1" | b"PSG1" | b"SFI0" | b"PSV0"
            )),
            "external container metadata"
        );
        let program =
            DxilProgram::parse(container.part(FourCC::DXIL).context("missing DXIL")?.data)?;
        ensure!(
            program.is_standard_layout(),
            "expected translated host DXIL"
        );
        let bc = program.bitcode;
        let passes = [
            w!("-opt-mod-passes"),
            w!("-simplifycfg"),
            w!("-instcombine"),
            w!("-early-cse"),
            w!("-gvn"),
            w!("-dce"),
            w!("-simplifycfg"),
        ];
        // SAFETY: CreateBlob copies the entire input slice. COM owns the intermediate buffers.
        // The validator signs the assembled bytes before we copy them out.
        unsafe {
            let input =
                self.utils
                    .CreateBlob(bc.as_ptr().cast(), bc.len().try_into()?, DXC_CP_ACP)?;
            let mut module = None;
            self.opt.RunOptimizer(&input, &passes, &mut module, None)?;
            let assembly = self
                .assembler
                .AssembleToContainer(&module.context("no optimizer result")?)?;
            let blob = result(&assembly)?;
            let validation = self
                .validator
                .Validate(&blob, DxcValidatorFlags_InPlaceEdit)?;
            let checked = result(&validation)?;
            Ok(blob_bytes(&checked).to_vec())
        }
    }
}

fn blob_bytes(blob: &IDxcBlob) -> &[u8] {
    // SAFETY: the interface owns its buffer for this borrow.
    unsafe {
        if blob.GetBufferSize() == 0 {
            return &[];
        }
        std::slice::from_raw_parts(blob.GetBufferPointer().cast(), blob.GetBufferSize())
    }
}

fn result(op: &IDxcOperationResult) -> Result<IDxcBlob> {
    // SAFETY: the completed SDK operation owns its result and diagnostic blobs.
    unsafe {
        if let Err(status) = op.GetStatus()?.ok() {
            let details = op.GetErrorBuffer()?.cast::<IDxcBlob>()?;
            return Err(anyhow!(
                "{status}: {}",
                String::from_utf8_lossy(blob_bytes(&details)).trim_end_matches('\0')
            ));
        }
        Ok(op.GetResult()?)
    }
}

pub fn run(sdk: &Path, mut paths: Vec<PathBuf>) -> Result<()> {
    let shader_dir = victory_core::Config::load()?.cache_subdir("shaders")?;
    let cache = OptimizedShaders::open(&shader_dir)?;
    let optimizer = Optimizer::open(sdk)?;
    if paths.is_empty() {
        paths.push(shader_dir.clone());
    }
    let mut optimized = 0;
    let mut existing = 0;
    let mut rejected = 0;
    let mut input_bytes = 0u64;
    let mut output_bytes = 0u64;
    for root in paths {
        let entries = walkdir::WalkDir::new(root)
            .into_iter()
            .filter_entry(|entry| entry.file_name() != "llvm-v1");
        for entry in entries {
            let entry = entry?;
            if !entry.file_type().is_file() {
                continue;
            }
            let path = entry.path();
            let raw = path
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("dxil"));
            let record = path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| {
                    name.len() == 62 && name.bytes().all(|c| c.is_ascii_hexdigit())
                });
            if !raw && !record {
                continue;
            }
            let bytes = std::fs::read(path)?;
            let input = if raw {
                bytes
            } else if let Some(dxil) = cached_dxil(&bytes) {
                dxil
            } else {
                continue;
            };
            if cache.read(&input).is_some() {
                existing += 1;
                continue;
            }
            match optimizer.optimize(&input) {
                Ok(output) => {
                    cache.store(&input, &output)?;
                    optimized += 1;
                    input_bytes += input.len() as u64;
                    output_bytes += output.len() as u64;
                }
                Err(error) => {
                    rejected += 1;
                    if rejected <= 5 {
                        eprintln!("{}: {error}", path.display());
                    }
                }
            }
        }
    }
    println!(
        "{optimized} optimized, {existing} already cached, {rejected} retained originals; {input_bytes} -> {output_bytes} container bytes"
    );
    ensure!(optimized + existing > 0, "no usable host shaders");
    Ok(())
}
