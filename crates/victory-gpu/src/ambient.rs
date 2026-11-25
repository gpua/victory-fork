//! Fallback for the named screen-probe temporal resolve in GoodGame.
//! When the ray-traced probes lack data, the resolve can leave invalid colors.
//! Replace its packed indirect diffuse output with a uniform tint. The game
//! still applies surface color and direct light. Set VICTORY_AMBIENT=off to
//! keep the original resolve output.

use std::sync::{Mutex, OnceLock};
use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D::ID3DBlob;
use windows::Win32::Graphics::Direct3D12::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::core::s;

use crate::host::{HostList, host};
use crate::object::This;

/// The programs it follows, by the start of their name.
pub(crate) const PROGRAM: &[u8] = b"CS_ScreenProbes_TemporalResolve";
/// Where they write the indirect diffuse light (R9G9B9E5 as 32-bit
/// integers): root parameter and place in its table.
pub(crate) const LIGHT: (u8, u32) = (1, 176);

/// A finite nonnegative intensity explicitly enables the fallback.
pub(crate) fn level() -> Option<f32> {
    static LEVEL: OnceLock<Option<f32>> = OnceLock::new();
    *LEVEL.get_or_init(|| {
        std::env::var("VICTORY_AMBIENT")
            .ok()?
            .trim()
            .parse::<f32>()
            .ok()
            .filter(|level| level.is_finite() && *level >= 0.0)
    })
}

const SOURCE: &str = r#"
#define RS "RootConstants(num32BitConstants=4, b0), DescriptorTable(UAV(u0))"
cbuffer Floor : register(b0) { float level; float3 tint; };
RWTexture2D<uint> light : register(u0);
uint pack(float3 c) {
    c = clamp(c, 0.0, 65408.0);
    float most = max(c.r, max(c.g, c.b));
    int e = clamp(int(floor(log2(max(most, 1e-9)))) + 16, 0, 31);
    float scale = exp2(float(24 - e));
    if (most * scale >= 511.5) { e = min(e + 1, 31); scale = exp2(float(24 - e)); }
    uint3 m = min(uint3(c * scale + 0.5), 511u);
    return m.r | (m.g << 9) | (m.b << 18) | (uint(e) << 27);
}
[RootSignature(RS)]
[numthreads(8, 8, 1)]
void main(uint3 at : SV_DispatchThreadID) {
    uint w, h;
    light.GetDimensions(w, h);
    if (at.x >= w || at.y >= h) return;
    // Missing ray data can leave invalid colors in the resolve output.
    light[at.xy] = pack(tint * level);
}
"#;

struct Pass {
    root: ID3D12RootSignature,
    pso: ID3D12PipelineState,
    view: Mutex<Option<(ID3D12Resource, ID3D12DescriptorHeap)>>,
}

impl Pass {
    /// Reuse an immutable descriptor. The recording list retains each heap it binds.
    fn view(
        &self,
        device: &ID3D12Device,
        light: &ID3D12Resource,
    ) -> windows::core::Result<ID3D12DescriptorHeap> {
        let mut cached = self.view.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((resource, heap)) = cached.as_ref()
            && resource == light
        {
            return Ok(heap.clone());
        }
        // SAFETY: the texture supports a typed 2D UAV. Only this new heap is written.
        let heap: ID3D12DescriptorHeap = unsafe {
            let heap: ID3D12DescriptorHeap =
                device.CreateDescriptorHeap(&D3D12_DESCRIPTOR_HEAP_DESC {
                    Type: D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,
                    NumDescriptors: 1,
                    Flags: D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE,
                    NodeMask: 0,
                })?;
            let view = D3D12_UNORDERED_ACCESS_VIEW_DESC {
                Format: DXGI_FORMAT_R32_UINT,
                ViewDimension: D3D12_UAV_DIMENSION_TEXTURE2D,
                ..Default::default()
            };
            device.CreateUnorderedAccessView(
                light,
                None::<&ID3D12Resource>,
                Some(&view),
                heap.GetCPUDescriptorHandleForHeapStart(),
            );
            heap
        };
        *cached = Some((light.clone(), heap.clone()));
        Ok(heap)
    }
}

fn make(device: &ID3D12Device) -> windows::core::Result<Pass> {
    let mut code: Option<ID3DBlob> = None;
    let mut errors: Option<ID3DBlob> = None;
    // SAFETY: the source and names live through the call; out pointers ours.
    let compiled = unsafe {
        D3DCompile(
            SOURCE.as_ptr().cast(),
            SOURCE.len(),
            s!("ambient"),
            None,
            None,
            s!("main"),
            s!("cs_5_1"),
            0,
            0,
            &mut code,
            Some(&mut errors),
        )
    };
    let text = |b: &ID3DBlob| {
        // SAFETY: the blob's own bytes.
        unsafe { std::slice::from_raw_parts(b.GetBufferPointer().cast::<u8>(), b.GetBufferSize()) }
            .to_vec()
    };
    if let Err(e) = compiled {
        if let Some(errors) = errors {
            tracing::warn!("ambient light: {}", String::from_utf8_lossy(&text(&errors)));
        }
        return Err(e);
    }
    let code = text(&code.ok_or_else(windows::core::Error::empty)?);
    // SAFETY: the root signature is inside the shader; plain creations.
    unsafe {
        let root: ID3D12RootSignature = device.CreateRootSignature(0, &code)?;
        let pso = device.CreateComputePipelineState(&D3D12_COMPUTE_PIPELINE_STATE_DESC {
            pRootSignature: std::mem::transmute_copy(&root),
            CS: D3D12_SHADER_BYTECODE {
                pShaderBytecode: code.as_ptr().cast(),
                BytecodeLength: code.len(),
            },
            ..Default::default()
        })?;
        Ok(Pass {
            root,
            pso,
            view: Mutex::new(None),
        })
    }
}

fn uav_barrier(list: &ID3D12GraphicsCommandList) {
    let barrier = D3D12_RESOURCE_BARRIER {
        Type: D3D12_RESOURCE_BARRIER_TYPE_UAV,
        ..Default::default()
    };
    // SAFETY: an open list; the barrier names no resource.
    unsafe { list.ResourceBarrier(&[barrier]) };
}

/// Replace the resolve output. Return true if this pass changed host bindings.
pub(crate) fn raise(list: &HostList, light: This) -> bool {
    let (Some(level), Some(host)) = (level(), host()) else {
        return false;
    };
    let Some(light) = crate::resource::host_resource(light) else {
        return false;
    };
    static PASS: OnceLock<Option<Pass>> = OnceLock::new();
    let Some(pass) = PASS
        .get_or_init(|| {
            make(&host.device)
                .inspect_err(|e| tracing::warn!(error = %e, "no ambient light"))
                .ok()
        })
        .as_ref()
    else {
        return false;
    };
    // SAFETY: a live resource.
    let d = unsafe { light.GetDesc() };
    static ONCE: std::sync::Once = std::sync::Once::new();
    if d.Format != DXGI_FORMAT_R32_UINT
        || d.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D
        || d.DepthOrArraySize != 1
        || d.SampleDesc.Count != 1
        || !d.Flags.contains(D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)
    {
        ONCE.call_once(|| tracing::warn!(format = d.Format.0, "no ambient light: another format"));
        return false;
    }
    let Ok(view) = pass.view(&host.device, light) else {
        return false;
    };
    ONCE.call_once(|| tracing::info!(target: "victory_gpu::host", level, width = d.Width, height = d.Height, "ambient resolve fallback"));
    list.keep(&view);
    list.keep(light);
    let l = &list.list;
    uav_barrier(l);
    // SAFETY: an open list; a live texture the list keeps, in the
    // unordered-access state as the program before left it; a view in a slot
    // no list in flight reads.
    unsafe {
        let gpu = view.GetGPUDescriptorHandleForHeapStart();
        l.SetDescriptorHeaps(&[Some(view.clone())]);
        l.SetComputeRootSignature(&pass.root);
        l.SetPipelineState(&pass.pso);
        let constants = [level, 1.0f32, 0.98, 0.94];
        l.SetComputeRoot32BitConstants(0, 4, constants.as_ptr().cast(), 0);
        l.SetComputeRootDescriptorTable(1, gpu);
        l.Dispatch((d.Width as u32).div_ceil(8), d.Height.div_ceil(8), 1);
    }
    uav_barrier(l);
    true
}

#[cfg(test)]
mod tests {
    /// The pass compiles with the system's compiler and the host takes it.
    #[test]
    fn ambient_pass_builds() {
        let Some(host) = crate::host::host() else {
            return;
        };
        super::make(&host.device).expect("the ambient pass");
    }
}
