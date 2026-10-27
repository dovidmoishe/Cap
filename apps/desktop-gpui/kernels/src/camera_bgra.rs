//! Limited-range BT.601 Y'CbCr to BGRA for the non-macOS camera preview.
//!
//! The integers match the renderer's BT.601 conversion: `c = Y - 16`,
//! `d = Cb - 128`, `e = Cr - 128`, luma gain `298`, chroma gains
//! `(rv, gu, gv, bu) = (409, 100, 208, 516)`, then `+ 128` and an arithmetic
//! `>> 8`, clamped to `0..=255`. Output byte order is B, G, R, A.
//!
//! A 4:2:2 pair (YUYV or UYVY) shares one Cb/Cr across two horizontal pixels,
//! and every row has its own chroma. NV12 keeps that horizontal siting: swscale
//! leaves full chroma interpolation off for BGRA, so each pair keeps its own
//! Cb/Cr. Vertically, swscale's `SWS_BILINEAR` 2× chroma upsample is quantized
//! by its packed converter to either one chroma row or a rounded average of two
//! (`(a + b + 1) >> 1`). [`vertical_tap`] is that decision, including the
//! 1-tap collapse when the chroma plane is shorter than four rows. The two
//! matrices still are not byte-identical, so a channel can move by a few codes.
//! On Linux the same image is what a processed camera recording publishes. The
//! scalar function is the spec; SSSE3 and AVX2 match it exactly, including odd
//! widths and padded strides. Scaled previews and every other pixel format stay
//! on swscale.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Packed422 {
    Yuyv,
    Uyvy,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Level {
    Scalar,
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    Ssse3,
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    Avx2,
}

pub fn packed422_to_bgra(
    data: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    format: Packed422,
    mirrored: bool,
) -> Option<Vec<u8>> {
    let sample_bytes = packed_sample_bytes(width)?;
    let mut pixels = alloc_bgra(data, width, height, stride, sample_bytes)?;
    store_packed422(data, &mut pixels, width, height, stride, format, mirrored)?;
    Some(pixels)
}

/// Convert into an existing BGRA buffer of `width * height * 4` bytes.
///
/// The preview moves each frame into a new `RenderImage`, so the allocating
/// entry point is the cost of a frame. This writes a buffer the caller already
/// owns.
pub fn store_packed422(
    data: &[u8],
    pixels: &mut [u8],
    width: usize,
    height: usize,
    stride: usize,
    format: Packed422,
    mirrored: bool,
) -> Option<()> {
    let sample_bytes = packed_sample_bytes(width)?;
    let output_len = checked_output_len(width, height)?;
    if pixels.len() < output_len || stride < sample_bytes {
        return None;
    }
    let plane_len = stride.checked_mul(height)?;
    if data.len() < plane_len {
        return None;
    }
    write_packed(
        data,
        &mut pixels[..output_len],
        width,
        height,
        stride,
        format,
        detect(),
    );
    if mirrored {
        mirror_rows(&mut pixels[..output_len], width);
    }
    Some(())
}

pub fn nv12_to_bgra(
    y: &[u8],
    uv: &[u8],
    width: usize,
    height: usize,
    y_stride: usize,
    uv_stride: usize,
    mirrored: bool,
) -> Option<Vec<u8>> {
    let uv_bytes = width.next_multiple_of(2);
    let uv_rows = height.div_ceil(2);
    if y_stride < width || uv_stride < uv_bytes {
        return None;
    }
    let y_plane = y_stride.checked_mul(height)?;
    let uv_plane = uv_stride.checked_mul(uv_rows)?;
    if y.len() < y_plane || uv.len() < uv_plane {
        return None;
    }
    let mut pixels = alloc_bgra(y, width, height, y_stride, width)?;
    store_nv12(
        Nv12Planes {
            y,
            uv,
            y_stride,
            uv_stride,
        },
        &mut pixels,
        width,
        height,
        mirrored,
    )?;
    Some(pixels)
}

fn store_nv12(
    planes: Nv12Planes<'_>,
    pixels: &mut [u8],
    width: usize,
    height: usize,
    mirrored: bool,
) -> Option<()> {
    let uv_bytes = width.next_multiple_of(2);
    let uv_rows = height.div_ceil(2);
    let output_len = checked_output_len(width, height)?;
    if pixels.len() < output_len || planes.y_stride < width || planes.uv_stride < uv_bytes {
        return None;
    }
    let y_plane = planes.y_stride.checked_mul(height)?;
    let uv_plane = planes.uv_stride.checked_mul(uv_rows)?;
    if planes.y.len() < y_plane || planes.uv.len() < uv_plane {
        return None;
    }
    write_nv12(planes, &mut pixels[..output_len], width, height, detect());
    if mirrored {
        mirror_rows(&mut pixels[..output_len], width);
    }
    Some(())
}

fn detect() -> Level {
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    {
        if is_x86_feature_detected!("avx2") {
            return Level::Avx2;
        }
        if is_x86_feature_detected!("ssse3") {
            return Level::Ssse3;
        }
    }
    Level::Scalar
}

fn packed_sample_bytes(width: usize) -> Option<usize> {
    width.checked_next_multiple_of(2)?.checked_mul(2)
}

fn checked_output_len(width: usize, height: usize) -> Option<usize> {
    if width == 0 || height == 0 {
        return None;
    }
    width.checked_mul(4)?.checked_mul(height)
}

struct Nv12Planes<'a> {
    y: &'a [u8],
    uv: &'a [u8],
    y_stride: usize,
    uv_stride: usize,
}

fn alloc_bgra(
    plane: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    row_samples: usize,
) -> Option<Vec<u8>> {
    if width == 0 || height == 0 || stride < row_samples {
        return None;
    }
    let plane_len = stride.checked_mul(height)?;
    if plane.len() < plane_len {
        return None;
    }
    let output_len = width.checked_mul(4)?.checked_mul(height)?;
    // `MaybeUninit<u8>` is the buffer clippy accepts here. Row writers store every
    // byte before the caller reads the `Vec<u8>`.
    let mut pixels: Vec<std::mem::MaybeUninit<u8>> = Vec::with_capacity(output_len);
    unsafe {
        pixels.set_len(output_len);
        Some(std::mem::transmute::<Vec<std::mem::MaybeUninit<u8>>, Vec<u8>>(pixels))
    }
}

fn write_packed(
    data: &[u8],
    pixels: &mut [u8],
    width: usize,
    height: usize,
    stride: usize,
    format: Packed422,
    level: Level,
) {
    let sample_bytes = packed_sample_bytes(width).expect("packed width fits");
    let row_bytes = width * 4;
    for row in 0..height {
        let src = &data[row * stride..row * stride + sample_bytes];
        let dst = &mut pixels[row * row_bytes..(row + 1) * row_bytes];
        convert_packed_row(src, dst, width, format, level);
    }
}

fn write_nv12(
    planes: Nv12Planes<'_>,
    pixels: &mut [u8],
    width: usize,
    height: usize,
    level: Level,
) {
    let uv_bytes = width.next_multiple_of(2);
    let row_bytes = width * 4;
    for row in 0..height {
        let (uv_index, blend) = vertical_tap(row, height);
        let y_row = &planes.y[row * planes.y_stride..row * planes.y_stride + width];
        let uv_start = uv_index * planes.uv_stride;
        let uv_row = &planes.uv[uv_start..uv_start + uv_bytes];
        let uv_next = if blend {
            let next = (uv_index + 1) * planes.uv_stride;
            &planes.uv[next..next + uv_bytes]
        } else {
            uv_row
        };
        let dst = &mut pixels[row * row_bytes..(row + 1) * row_bytes];
        convert_nv12_row(y_row, uv_row, uv_next, dst, width, blend, level);
    }
}

fn convert_packed_row(src: &[u8], dst: &mut [u8], width: usize, format: Packed422, level: Level) {
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    let done = match level {
        Level::Avx2 => {
            let simd = width & !7;
            if simd > 0 {
                unsafe { packed_avx2(src, dst, simd, format) };
            }
            let mut next = simd;
            let ssse3 = simd + ((width - simd) & !3);
            if ssse3 > simd {
                unsafe { packed_ssse3(src, dst, simd, ssse3, format) };
                next = ssse3;
            }
            next
        }
        Level::Ssse3 => {
            let simd = width & !3;
            if simd > 0 {
                unsafe { packed_ssse3(src, dst, 0, simd, format) };
            }
            simd
        }
        Level::Scalar => 0,
    };
    #[cfg(not(any(target_arch = "x86", target_arch = "x86_64")))]
    let done = {
        let _ = level;
        0
    };
    packed_scalar(src, dst, done, width, format);
}

fn convert_nv12_row(
    y: &[u8],
    uv0: &[u8],
    uv1: &[u8],
    dst: &mut [u8],
    width: usize,
    blend: bool,
    level: Level,
) {
    #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
    let done = match level {
        Level::Avx2 => {
            let simd = width & !7;
            if simd > 0 {
                unsafe {
                    if blend {
                        nv12_avx2::<true>(y, uv0, uv1, dst, simd);
                    } else {
                        nv12_avx2::<false>(y, uv0, uv1, dst, simd);
                    }
                }
            }
            let mut next = simd;
            let ssse3 = simd + ((width - simd) & !3);
            if ssse3 > simd {
                unsafe {
                    if blend {
                        nv12_ssse3::<true>(y, uv0, uv1, dst, simd, ssse3);
                    } else {
                        nv12_ssse3::<false>(y, uv0, uv1, dst, simd, ssse3);
                    }
                }
                next = ssse3;
            }
            next
        }
        Level::Ssse3 => {
            let simd = width & !3;
            if simd > 0 {
                unsafe {
                    if blend {
                        nv12_ssse3::<true>(y, uv0, uv1, dst, 0, simd);
                    } else {
                        nv12_ssse3::<false>(y, uv0, uv1, dst, 0, simd);
                    }
                }
            }
            simd
        }
        Level::Scalar => 0,
    };
    #[cfg(not(any(target_arch = "x86", target_arch = "x86_64")))]
    let done = {
        let _ = (level, blend, uv1);
        0
    };
    nv12_scalar(y, uv0, uv1, dst, done, width, blend);
}

#[inline(always)]
fn bt601_bgra(y: u8, cb: u8, cr: u8) -> [u8; 4] {
    let c = i32::from(y) - 16;
    let d = i32::from(cb) - 128;
    let e = i32::from(cr) - 128;
    let luma = 298 * c;
    let r = clamp8((luma + 409 * e + 128) >> 8);
    let g = clamp8((luma - 100 * d - 208 * e + 128) >> 8);
    let b = clamp8((luma + 516 * d + 128) >> 8);
    [b, g, r, 255]
}

#[inline(always)]
fn clamp8(value: i32) -> u8 {
    value.clamp(0, 255) as u8
}

fn packed_scalar(src: &[u8], dst: &mut [u8], start: usize, end: usize, format: Packed422) {
    let mut x = start;
    while x < end {
        let base = (x & !1) * 2;
        let (y, cb, cr) = match format {
            Packed422::Yuyv => (src[x * 2], src[base + 1], src[base + 3]),
            Packed422::Uyvy => (src[x * 2 + 1], src[base], src[base + 2]),
        };
        dst[x * 4..x * 4 + 4].copy_from_slice(&bt601_bgra(y, cb, cr));
        x += 1;
    }
}

fn nv12_scalar(
    y: &[u8],
    uv0: &[u8],
    uv1: &[u8],
    dst: &mut [u8],
    start: usize,
    end: usize,
    blend: bool,
) {
    let mut x = start;
    while x < end {
        let uv_index = x & !1;
        let (cb, cr) = if blend {
            (
                avg_u8(uv0[uv_index], uv1[uv_index]),
                avg_u8(uv0[uv_index + 1], uv1[uv_index + 1]),
            )
        } else {
            (uv0[uv_index], uv0[uv_index + 1])
        };
        dst[x * 4..x * 4 + 4].copy_from_slice(&bt601_bgra(y[x], cb, cr));
        x += 1;
    }
}

#[inline(always)]
fn avg_u8(a: u8, b: u8) -> u8 {
    ((u16::from(a) + u16::from(b) + 1) >> 1) as u8
}

/// Which NV12 chroma row(s) feed luma row `y` for a 1:1 BGRA preview.
///
/// `true` means a rounded average of `pos` and `pos + 1`. The arithmetic is
/// swscale's bilinear vertical filter (`src`/`dst` chroma position 128, so the
/// sample sits in the middle of the 2×2 block) after its packed-RGB converter
/// collapses a 2-tap weight to "under half → this row, otherwise 50/50".
/// Planes shorter than four chroma rows never get a second tap.
fn vertical_tap(y: usize, height: usize) -> (usize, bool) {
    let src_h = height.div_ceil(2);
    let x_inc = (((src_h as u64) << 16) + ((height as u64) >> 1)) / (height as u64);
    let filter_size = 3.min(src_h.saturating_sub(2)).max(1);
    let x = (x_inc as i64 - 65_536) + (y as i64) * (2 * x_inc as i64);
    let mut pos = (x - (filter_size as i64 - 2) * 65_536) / (1 << 17);
    let mut coeffs = [0i64; 3];
    let mut count = 0usize;
    for sample in pos..pos + filter_size as i64 {
        let delta = sample * (1i64 << 17) - x;
        let mut coeff = (1i64 << 30) - (delta.abs() << 13);
        if coeff < 0 {
            coeff = 0;
        }
        coeffs[count] = coeff;
        count += 1;
    }
    let cutoff = (1i64 << 30) / 500;
    while count > 1 && coeffs[0] <= cutoff {
        let mut index = 1;
        while index < count {
            coeffs[index - 1] = coeffs[index];
            index += 1;
        }
        count -= 1;
        pos += 1;
    }
    while count > 1 && coeffs[count - 1] <= cutoff {
        count -= 1;
    }
    if src_h <= 3 {
        return (pos.clamp(0, src_h as i64 - 1) as usize, false);
    }
    let mut pair = [coeffs[0], if count > 1 { coeffs[1] } else { 0 }];
    if pos < 0 {
        pos = 0;
    }
    if pos + 2 > src_h as i64 {
        let shift = pos + (2 - src_h as i64).min(0);
        let mut acc = 0i64;
        let mut tmp = pair;
        let mut index = 1i64;
        while index >= 0 {
            if pos + index >= src_h as i64 {
                acc += tmp[index as usize];
                tmp[index as usize] = 0;
            }
            index -= 1;
        }
        let mut shifted = [0i64; 2];
        index = 1;
        while index >= 0 {
            if index >= shift {
                shifted[index as usize] = tmp[(index - shift) as usize];
            }
            index -= 1;
        }
        pos -= shift;
        let edge = src_h as i64 - 1 - pos;
        if (0..2).contains(&edge) {
            shifted[edge as usize] += acc;
        }
        pair = shifted;
    }
    let pos = pos.clamp(0, src_h as i64 - 1);
    let total = pair[0] + pair[1];
    let norm = ((total + 2048) / 4096).max(1);
    let second = if pair[1] > 0 {
        (pair[1] + (norm >> 1)) / norm
    } else {
        (pair[1] - (norm >> 1)) / norm
    };
    (pos as usize, second >= 2048)
}

fn mirror_rows(pixels: &mut [u8], width: usize) {
    if width < 2 {
        return;
    }
    let row_bytes = width * 4;
    for row in pixels.chunks_exact_mut(row_bytes) {
        let (row_pixels, _) = row.as_chunks_mut::<4>();
        let half = row_pixels.len() / 2;
        let mut index = 0;
        while index < half {
            row_pixels.swap(index, row_pixels.len() - 1 - index);
            index += 1;
        }
    }
}

#[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
use std::arch::is_x86_feature_detected;

#[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
mod simd {
    use super::Packed422;
    #[cfg(target_arch = "x86")]
    use std::arch::x86::*;
    #[cfg(target_arch = "x86_64")]
    use std::arch::x86_64::*;

    #[target_feature(enable = "avx2")]
    pub(super) unsafe fn packed_avx2(src: &[u8], dst: &mut [u8], pixels: usize, format: Packed422) {
        unsafe {
            let coeffs = AvxCoeffs::new();
            let (y_shuf, u_shuf, v_shuf) = avx_shuffles(format);
            let mut x = 0;
            while x < pixels {
                let packed = _mm_loadu_si128(src.as_ptr().add(x * 2).cast());
                store_avx2(
                    dst.as_mut_ptr().add(x * 4),
                    _mm_shuffle_epi8(packed, y_shuf),
                    _mm_shuffle_epi8(packed, u_shuf),
                    _mm_shuffle_epi8(packed, v_shuf),
                    &coeffs,
                );
                x += 8;
            }
        }
    }

    #[target_feature(enable = "ssse3")]
    pub(super) unsafe fn packed_ssse3(
        src: &[u8],
        dst: &mut [u8],
        start: usize,
        end: usize,
        format: Packed422,
    ) {
        unsafe {
            let coeffs = SseCoeffs::new();
            let (y_shuf, u_shuf, v_shuf) = sse_shuffles(format);
            let mut x = start;
            while x < end {
                let packed = _mm_loadl_epi64(src.as_ptr().add(x * 2).cast());
                store_ssse3(
                    dst.as_mut_ptr().add(x * 4),
                    _mm_shuffle_epi8(packed, y_shuf),
                    _mm_shuffle_epi8(packed, u_shuf),
                    _mm_shuffle_epi8(packed, v_shuf),
                    &coeffs,
                );
                x += 4;
            }
        }
    }

    #[target_feature(enable = "avx2")]
    pub(super) unsafe fn nv12_avx2<const BLEND: bool>(
        y: &[u8],
        uv0: &[u8],
        uv1: &[u8],
        dst: &mut [u8],
        pixels: usize,
    ) {
        unsafe {
            let coeffs = AvxCoeffs::new();
            let u_shuf = _mm_setr_epi8(0, 2, 4, 6, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1);
            let v_shuf = _mm_setr_epi8(1, 3, 5, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1);
            let mut x = 0;
            while x < pixels {
                let luma = _mm_loadl_epi64(y.as_ptr().add(x).cast());
                let chroma = nv12_chroma_avx::<BLEND>(uv0.as_ptr().add(x), uv1.as_ptr().add(x));
                store_avx2(
                    dst.as_mut_ptr().add(x * 4),
                    luma,
                    _mm_shuffle_epi8(chroma, u_shuf),
                    _mm_shuffle_epi8(chroma, v_shuf),
                    &coeffs,
                );
                x += 8;
            }
        }
    }

    #[target_feature(enable = "avx2")]
    unsafe fn nv12_chroma_avx<const BLEND: bool>(uv0: *const u8, uv1: *const u8) -> __m128i {
        unsafe {
            let row = _mm_loadl_epi64(uv0.cast());
            if BLEND {
                _mm_avg_epu8(row, _mm_loadl_epi64(uv1.cast()))
            } else {
                let _ = uv1;
                row
            }
        }
    }

    #[target_feature(enable = "ssse3")]
    pub(super) unsafe fn nv12_ssse3<const BLEND: bool>(
        y: &[u8],
        uv0: &[u8],
        uv1: &[u8],
        dst: &mut [u8],
        start: usize,
        end: usize,
    ) {
        unsafe {
            let coeffs = SseCoeffs::new();
            let u_shuf = _mm_setr_epi8(0, 0, 2, 2, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1);
            let v_shuf = _mm_setr_epi8(1, 1, 3, 3, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1);
            let y_shuf = _mm_setr_epi8(0, 1, 2, 3, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1);
            let mut x = start;
            while x < end {
                let luma = _mm_cvtsi32_si128(read_u32(y.as_ptr().add(x)) as i32);
                let chroma = nv12_chroma_sse::<BLEND>(uv0.as_ptr().add(x), uv1.as_ptr().add(x));
                store_ssse3(
                    dst.as_mut_ptr().add(x * 4),
                    _mm_shuffle_epi8(luma, y_shuf),
                    _mm_shuffle_epi8(chroma, u_shuf),
                    _mm_shuffle_epi8(chroma, v_shuf),
                    &coeffs,
                );
                x += 4;
            }
        }
    }

    #[target_feature(enable = "sse2")]
    unsafe fn nv12_chroma_sse<const BLEND: bool>(uv0: *const u8, uv1: *const u8) -> __m128i {
        let row = _mm_cvtsi32_si128(read_u32(uv0) as i32);
        if BLEND {
            _mm_avg_epu8(row, _mm_cvtsi32_si128(read_u32(uv1) as i32))
        } else {
            let _ = uv1;
            row
        }
    }

    #[target_feature(enable = "sse2")]
    unsafe fn avx_shuffles(format: Packed422) -> (__m128i, __m128i, __m128i) {
        match format {
            Packed422::Yuyv => (
                _mm_setr_epi8(0, 2, 4, 6, 8, 10, 12, 14, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(1, 5, 9, 13, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(3, 7, 11, 15, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
            ),
            Packed422::Uyvy => (
                _mm_setr_epi8(1, 3, 5, 7, 9, 11, 13, 15, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(0, 4, 8, 12, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(2, 6, 10, 14, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
            ),
        }
    }

    #[target_feature(enable = "sse2")]
    unsafe fn sse_shuffles(format: Packed422) -> (__m128i, __m128i, __m128i) {
        match format {
            Packed422::Yuyv => (
                _mm_setr_epi8(0, 2, 4, 6, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(1, 1, 5, 5, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(3, 3, 7, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
            ),
            Packed422::Uyvy => (
                _mm_setr_epi8(1, 3, 5, 7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(0, 0, 4, 4, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
                _mm_setr_epi8(2, 2, 6, 6, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1),
            ),
        }
    }

    struct AvxCoeffs {
        rv: __m256i,
        gu: __m256i,
        gv: __m256i,
        bu: __m256i,
        bias128: __m256i,
        y16: __m256i,
        gain: __m256i,
        alpha: __m256i,
        zero: __m256i,
        max: __m256i,
        dup: __m256i,
    }

    impl AvxCoeffs {
        #[target_feature(enable = "avx2")]
        unsafe fn new() -> Self {
            Self {
                rv: _mm256_set1_epi32(409),
                gu: _mm256_set1_epi32(100),
                gv: _mm256_set1_epi32(208),
                bu: _mm256_set1_epi32(516),
                bias128: _mm256_set1_epi32(128),
                y16: _mm256_set1_epi32(16),
                gain: _mm256_set1_epi32(298),
                alpha: _mm256_set1_epi32(0xFF00_0000u32 as i32),
                zero: _mm256_setzero_si256(),
                max: _mm256_set1_epi32(255),
                dup: _mm256_setr_epi32(0, 0, 1, 1, 2, 2, 3, 3),
            }
        }
    }

    struct SseCoeffs {
        rv: __m128i,
        gu: __m128i,
        gv: __m128i,
        bu: __m128i,
        bias128: __m128i,
        y16: __m128i,
        gain: __m128i,
        alpha: __m128i,
        zero: __m128i,
        max: __m128i,
    }

    impl SseCoeffs {
        #[target_feature(enable = "sse2")]
        unsafe fn new() -> Self {
            Self {
                rv: _mm_set1_epi32(409),
                gu: _mm_set1_epi32(100),
                gv: _mm_set1_epi32(208),
                bu: _mm_set1_epi32(516),
                bias128: _mm_set1_epi32(128),
                y16: _mm_set1_epi32(16),
                gain: _mm_set1_epi32(298),
                alpha: _mm_set1_epi32(0xFF00_0000u32 as i32),
                zero: _mm_setzero_si128(),
                max: _mm_set1_epi32(255),
            }
        }
    }

    #[target_feature(enable = "avx2")]
    unsafe fn store_avx2(out: *mut u8, y8: __m128i, u4: __m128i, v4: __m128i, coeffs: &AvxCoeffs) {
        unsafe {
            let luma = _mm256_mullo_epi32(
                _mm256_sub_epi32(_mm256_cvtepu8_epi32(y8), coeffs.y16),
                coeffs.gain,
            );
            let d = _mm256_sub_epi32(
                _mm256_permutevar8x32_epi32(_mm256_cvtepu8_epi32(u4), coeffs.dup),
                coeffs.bias128,
            );
            let e = _mm256_sub_epi32(
                _mm256_permutevar8x32_epi32(_mm256_cvtepu8_epi32(v4), coeffs.dup),
                coeffs.bias128,
            );
            let red = _mm256_add_epi32(_mm256_mullo_epi32(coeffs.rv, e), coeffs.bias128);
            let green = _mm256_sub_epi32(
                coeffs.bias128,
                _mm256_add_epi32(
                    _mm256_mullo_epi32(coeffs.gu, d),
                    _mm256_mullo_epi32(coeffs.gv, e),
                ),
            );
            let blue = _mm256_add_epi32(_mm256_mullo_epi32(coeffs.bu, d), coeffs.bias128);
            let r = clamp_avx2(_mm256_add_epi32(luma, red), coeffs);
            let g = clamp_avx2(_mm256_add_epi32(luma, green), coeffs);
            let b = clamp_avx2(_mm256_add_epi32(luma, blue), coeffs);
            let pixels = _mm256_or_si256(
                b,
                _mm256_or_si256(
                    _mm256_slli_epi32(g, 8),
                    _mm256_or_si256(_mm256_slli_epi32(r, 16), coeffs.alpha),
                ),
            );
            _mm256_storeu_si256(out.cast(), pixels);
        }
    }

    #[target_feature(enable = "avx2")]
    unsafe fn clamp_avx2(sum: __m256i, coeffs: &AvxCoeffs) -> __m256i {
        let shifted = _mm256_srai_epi32(sum, 8);
        _mm256_min_epi32(_mm256_max_epi32(shifted, coeffs.zero), coeffs.max)
    }

    #[target_feature(enable = "ssse3")]
    unsafe fn store_ssse3(out: *mut u8, y4: __m128i, u4: __m128i, v4: __m128i, coeffs: &SseCoeffs) {
        unsafe {
            let y32 = bytes_to_i32(y4);
            let luma = mullo_epi32(_mm_sub_epi32(y32, coeffs.y16), coeffs.gain);
            let d = _mm_sub_epi32(bytes_to_i32(u4), coeffs.bias128);
            let e = _mm_sub_epi32(bytes_to_i32(v4), coeffs.bias128);
            let red = _mm_add_epi32(mullo_epi32(coeffs.rv, e), coeffs.bias128);
            let green = _mm_sub_epi32(
                coeffs.bias128,
                _mm_add_epi32(mullo_epi32(coeffs.gu, d), mullo_epi32(coeffs.gv, e)),
            );
            let blue = _mm_add_epi32(mullo_epi32(coeffs.bu, d), coeffs.bias128);
            let r = clamp_sse(_mm_add_epi32(luma, red), coeffs);
            let g = clamp_sse(_mm_add_epi32(luma, green), coeffs);
            let b = clamp_sse(_mm_add_epi32(luma, blue), coeffs);
            let pixels = _mm_or_si128(
                b,
                _mm_or_si128(
                    _mm_slli_epi32(g, 8),
                    _mm_or_si128(_mm_slli_epi32(r, 16), coeffs.alpha),
                ),
            );
            _mm_storeu_si128(out.cast(), pixels);
        }
    }

    #[target_feature(enable = "sse2")]
    unsafe fn bytes_to_i32(bytes: __m128i) -> __m128i {
        let zero = _mm_setzero_si128();
        _mm_unpacklo_epi16(_mm_unpacklo_epi8(bytes, zero), zero)
    }

    #[target_feature(enable = "sse2")]
    unsafe fn mullo_epi32(a: __m128i, b: __m128i) -> __m128i {
        let even = _mm_mul_epu32(a, b);
        let odd = _mm_mul_epu32(_mm_srli_si128(a, 4), _mm_srli_si128(b, 4));
        _mm_unpacklo_epi64(_mm_unpacklo_epi32(even, odd), _mm_unpackhi_epi32(even, odd))
    }

    #[target_feature(enable = "sse2")]
    unsafe fn clamp_sse(sum: __m128i, coeffs: &SseCoeffs) -> __m128i {
        let shifted = _mm_srai_epi32(sum, 8);
        let negative = _mm_cmpgt_epi32(coeffs.zero, shifted);
        let non_negative = _mm_andnot_si128(negative, shifted);
        let above = _mm_cmpgt_epi32(non_negative, coeffs.max);
        _mm_or_si128(
            _mm_andnot_si128(above, non_negative),
            _mm_and_si128(above, coeffs.max),
        )
    }

    fn read_u32(ptr: *const u8) -> u32 {
        unsafe { std::ptr::read_unaligned(ptr.cast()) }
    }
}

#[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
use simd::{nv12_avx2, nv12_ssse3, packed_avx2, packed_ssse3};

#[cfg(test)]
mod tests {
    use super::*;

    fn pattern(len: usize, seed: u32) -> Vec<u8> {
        let mut state = seed;
        (0..len)
            .map(|_| {
                state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                (state >> 24) as u8
            })
            .collect()
    }

    fn padded_rows(sample_bytes: usize, stride: usize, height: usize, seed: u32) -> Vec<u8> {
        let mut data = vec![0xA5; stride * height];
        let samples = pattern(sample_bytes * height, seed);
        for row in 0..height {
            let start = row * stride;
            data[start..start + sample_bytes]
                .copy_from_slice(&samples[row * sample_bytes..(row + 1) * sample_bytes]);
        }
        data
    }

    fn levels() -> Vec<Level> {
        let mut levels = vec![Level::Scalar];
        #[cfg(any(target_arch = "x86", target_arch = "x86_64"))]
        {
            if is_x86_feature_detected!("ssse3") {
                levels.push(Level::Ssse3);
            }
            if is_x86_feature_detected!("avx2") {
                levels.push(Level::Avx2);
            }
        }
        levels
    }

    fn assert_packed(format: Packed422, width: usize, height: usize, stride: usize) {
        let sample_bytes = width.next_multiple_of(2) * 2;
        let data = padded_rows(sample_bytes, stride, height, 0x1234_0000 | width as u32);
        let scalar = {
            let mut pixels = vec![0u8; width * 4 * height];
            write_packed(
                &data,
                &mut pixels,
                width,
                height,
                stride,
                format,
                Level::Scalar,
            );
            pixels
        };
        for level in levels() {
            let mut pixels = vec![0u8; width * 4 * height];
            write_packed(&data, &mut pixels, width, height, stride, format, level);
            assert_eq!(
                pixels, scalar,
                "{format:?} {width}x{height} stride {stride} {level:?}"
            );
        }
        let straight = packed422_to_bgra(&data, width, height, stride, format, false).unwrap();
        assert_eq!(straight, scalar, "{format:?} public {width}x{height}");
        let mirrored = packed422_to_bgra(&data, width, height, stride, format, true).unwrap();
        let mut expected = scalar;
        mirror_rows(&mut expected, width);
        assert_eq!(mirrored, expected, "{format:?} mirrored {width}x{height}");
    }

    fn assert_nv12(width: usize, height: usize, y_stride: usize, uv_stride: usize) {
        let uv_bytes = width.next_multiple_of(2);
        let uv_rows = height.div_ceil(2);
        let y = padded_rows(width, y_stride, height, 7);
        let uv = padded_rows(uv_bytes, uv_stride, uv_rows, 11);
        let scalar = {
            let mut pixels = vec![0u8; width * 4 * height];
            write_nv12(
                Nv12Planes {
                    y: &y,
                    uv: &uv,
                    y_stride,
                    uv_stride,
                },
                &mut pixels,
                width,
                height,
                Level::Scalar,
            );
            pixels
        };
        for level in levels() {
            let mut pixels = vec![0u8; width * 4 * height];
            write_nv12(
                Nv12Planes {
                    y: &y,
                    uv: &uv,
                    y_stride,
                    uv_stride,
                },
                &mut pixels,
                width,
                height,
                level,
            );
            assert_eq!(
                pixels, scalar,
                "nv12 {width}x{height} y {y_stride} uv {uv_stride} {level:?}"
            );
        }
        let public = nv12_to_bgra(&y, &uv, width, height, y_stride, uv_stride, false).unwrap();
        assert_eq!(public, scalar);
    }

    #[test]
    fn limited_range_grey_axis_is_exact() {
        assert_eq!(bt601_bgra(16, 128, 128), [0, 0, 0, 255]);
        assert_eq!(bt601_bgra(235, 128, 128), [255, 255, 255, 255]);
        assert_eq!(bt601_bgra(128, 128, 128), [130, 130, 130, 255]);
        assert_eq!(bt601_bgra(128, 16, 240), [0, 83, 255, 255]);
    }

    #[test]
    fn packed_formats_match_scalar_on_odd_widths_and_padded_strides() {
        for format in [Packed422::Yuyv, Packed422::Uyvy] {
            for (width, height, stride) in [
                (1, 1, 4),
                (2, 2, 4),
                (3, 2, 16),
                (7, 3, 32),
                (8, 1, 16),
                (15, 5, 64),
                (16, 2, 40),
            ] {
                assert_packed(format, width, height, stride);
            }
        }
    }

    fn vstep_planes(width: usize, height: usize) -> (Vec<u8>, Vec<u8>) {
        let y = vec![128u8; width * height];
        let uv_bytes = width.next_multiple_of(2);
        let mut uv = vec![0u8; uv_bytes * height.div_ceil(2)];
        for row in 0..height.div_ceil(2) {
            let (cb, cr) = if row % 2 == 0 { (16, 240) } else { (240, 16) };
            for col in (0..uv_bytes).step_by(2) {
                let index = row * uv_bytes + col;
                uv[index] = cb;
                uv[index + 1] = cr;
            }
        }
        (y, uv)
    }

    fn assert_row_color(pixels: &[u8], width: usize, row: usize, expected: [u8; 4]) {
        let start = row * width * 4;
        for pixel in pixels[start..start + width * 4].chunks_exact(4) {
            assert_eq!(pixel, expected, "row {row}");
        }
    }

    #[test]
    fn nv12_vertical_chroma_follows_swscale_bilinear_taps() {
        assert_eq!(
            (0..8).map(|y| vertical_tap(y, 8)).collect::<Vec<_>>(),
            vec![
                (0, false),
                (0, false),
                (0, true),
                (1, false),
                (1, true),
                (2, false),
                (2, true),
                (2, true),
            ]
        );
        assert_eq!(
            (0..6).map(|y| vertical_tap(y, 6)).collect::<Vec<_>>(),
            vec![
                (0, false),
                (0, false),
                (1, false),
                (1, false),
                (2, false),
                (2, false)
            ]
        );
        assert_eq!(vertical_tap(0, 3), (0, false));
        assert_eq!(vertical_tap(1, 3), (1, false));
        assert_eq!(vertical_tap(2, 3), (1, false));
        assert_eq!(vertical_tap(0, 1080), (0, false));
        assert_eq!(vertical_tap(1, 1080), (0, false));
        assert_eq!(vertical_tap(2, 1080), (0, true));
        assert_eq!(vertical_tap(1076, 1080), (537, true));
        assert_eq!(vertical_tap(1077, 1080), (538, false));
        assert_eq!(vertical_tap(1078, 1080), (538, true));
        assert_eq!(vertical_tap(1079, 1080), (538, true));

        let pure_a = [0, 83, 255, 255];
        let pure_b = [255, 178, 0, 255];
        let mixed = [130, 130, 130, 255];
        let (y, uv) = vstep_planes(15, 8);
        let pixels = nv12_to_bgra(&y, &uv, 15, 8, 15, 16, false).unwrap();
        for (row, color) in [pure_a, pure_a, mixed, pure_b, mixed, pure_a, mixed, mixed]
            .into_iter()
            .enumerate()
        {
            assert_row_color(&pixels, 15, row, color);
        }
        for level in levels() {
            let mut got = vec![0u8; pixels.len()];
            write_nv12(
                Nv12Planes {
                    y: &y,
                    uv: &uv,
                    y_stride: 15,
                    uv_stride: 16,
                },
                &mut got,
                15,
                8,
                level,
            );
            assert_eq!(got, pixels, "{level:?}");
        }

        let (y, uv) = vstep_planes(8, 6);
        let nearest = nv12_to_bgra(&y, &uv, 8, 6, 8, 8, false).unwrap();
        for (row, color) in [pure_a, pure_a, pure_b, pure_b, pure_a, pure_a]
            .into_iter()
            .enumerate()
        {
            assert_row_color(&nearest, 8, row, color);
        }
    }

    #[test]
    fn nv12_matches_scalar_on_odd_sizes_and_padded_strides() {
        for (width, height, y_stride, uv_stride) in [
            (1, 1, 1, 2),
            (2, 2, 8, 8),
            (3, 3, 16, 8),
            (7, 5, 16, 16),
            (8, 4, 8, 8),
            (15, 9, 32, 32),
            (16, 8, 32, 16),
        ] {
            assert_nv12(width, height, y_stride, uv_stride);
        }
    }

    #[test]
    fn short_or_empty_planes_are_rejected() {
        assert!(packed422_to_bgra(&[0; 8], 0, 2, 4, Packed422::Yuyv, false).is_none());
        assert!(packed422_to_bgra(&[0; 4], 2, 2, 4, Packed422::Yuyv, false).is_none());
        assert!(packed422_to_bgra(&[0; 8], 3, 1, 4, Packed422::Uyvy, false).is_none());
        assert!(nv12_to_bgra(&[0; 8], &[0; 2], 2, 4, 2, 2, false).is_none());
        assert!(nv12_to_bgra(&[16, 16], &[128, 128], 2, 1, 1, 2, false).is_none());
    }

    #[test]
    fn padding_bytes_do_not_change_active_pixels() {
        let tight =
            packed422_to_bgra(&[16, 128, 235, 128], 2, 1, 4, Packed422::Yuyv, false).unwrap();
        let mut padded = vec![0xA5; 16];
        padded[..4].copy_from_slice(&[16, 128, 235, 128]);
        let wide = packed422_to_bgra(&padded, 2, 1, 16, Packed422::Yuyv, false).unwrap();
        assert_eq!(tight, [0, 0, 0, 255, 255, 255, 255, 255]);
        assert_eq!(wide, tight);
    }
}
