//! Temporary Apple Silicon probe for the NV12 preview gap. Not part of the product.

use std::ffi::{CStr, c_char, c_int};

use cap_gpui_kernels::{camera_bgra, frame};
use ffmpeg::{
    format::Pixel,
    software::scaling::{Context, Flags},
};

unsafe extern "C" {
    fn av_version_info() -> *const c_char;
    fn av_get_cpu_flags() -> c_int;
    fn av_force_cpu_flags(flags: c_int);
}

fn main() {
    ffmpeg::init().expect("ffmpeg");
    let sws = ffmpeg::software::scaling::version();
    let util = ffmpeg::util::version();
    let info = unsafe { CStr::from_ptr(av_version_info()) };
    println!("GAPDIAG arch={}", std::env::consts::ARCH);
    println!("GAPDIAG av_version_info={}", info.to_string_lossy());
    println!(
        "GAPDIAG avutil_version={util} ({}.{}.{})",
        util >> 16,
        (util >> 8) & 0xff,
        util & 0xff
    );
    println!(
        "GAPDIAG swscale_version={sws} ({}.{}.{})",
        sws >> 16,
        (sws >> 8) & 0xff,
        sws & 0xff
    );
    println!(
        "GAPDIAG swscale_configuration={}",
        ffmpeg::software::scaling::configuration()
    );
    println!(
        "GAPDIAG avutil_configuration={}",
        ffmpeg::util::configuration()
    );
    println!("GAPDIAG cpu_flags_before={}", unsafe { av_get_cpu_flags() });
    println!("GAPDIAG simd_path={}", camera_bgra::simd_path());

    let mut source = ffmpeg::frame::Video::new(Pixel::NV12, 128, 16);
    println!(
        "GAPDIAG frame width={} height={} y_stride={} uv_stride={} y_len={} uv_len={}",
        source.width(),
        source.height(),
        source.stride(0),
        source.stride(1),
        source.data(0).len(),
        source.data(1).len()
    );
    for plane in 0..2 {
        for (index, byte) in source.data_mut(plane).iter_mut().enumerate() {
            *byte = index.wrapping_mul(17).wrapping_add(plane * 41) as u8;
        }
    }
    let kernel = camera_bgra::nv12_to_bgra(
        source.data(0),
        source.data(1),
        source.width() as usize,
        source.height() as usize,
        source.stride(0),
        source.stride(1),
        false,
    )
    .expect("kernel");

    report("BILINEAR", Flags::BILINEAR, &source, &kernel);
    report(
        "BILINEAR|ACCURATE_RND",
        Flags::BILINEAR | Flags::ACCURATE_RND,
        &source,
        &kernel,
    );
    report(
        "BILINEAR|ACCURATE_RND|FULL_CHR_H_INT",
        Flags::BILINEAR | Flags::ACCURATE_RND | Flags::FULL_CHR_H_INT,
        &source,
        &kernel,
    );
    report("POINT", Flags::POINT, &source, &kernel);

    unsafe { av_force_cpu_flags(0) };
    println!("GAPDIAG cpu_flags_forced={}", unsafe { av_get_cpu_flags() });
    report(
        "BILINEAR|force_cpu_flags_0",
        Flags::BILINEAR,
        &source,
        &kernel,
    );
}

fn report(name: &str, flags: Flags, source: &ffmpeg::frame::Video, kernel: &[u8]) {
    let mut scaler = Context::get(
        source.format(),
        source.width(),
        source.height(),
        Pixel::BGRA,
        source.width(),
        source.height(),
        flags,
    )
    .expect("swscale context");
    let mut converted = ffmpeg::frame::Video::empty();
    scaler.run(source, &mut converted).expect("swscale");
    let reference = frame::copy_rows(
        converted.data(0),
        source.width() as usize * 4,
        converted.stride(0),
        source.height() as usize,
        false,
    )
    .expect("tight rows");
    let mut max = 0u8;
    let mut at = 0usize;
    let mut over_three = 0usize;
    for (index, (kernel_byte, reference_byte)) in kernel.iter().zip(&reference).enumerate() {
        let delta = kernel_byte.abs_diff(*reference_byte);
        if delta > 3 {
            over_three += 1;
        }
        if delta > max {
            max = delta;
            at = index;
        }
    }
    let width = source.width() as usize;
    let x = (at / 4) % width;
    let y = at / (width * 4);
    let channel = ["B", "G", "R", "A"][at % 4];
    println!(
        "GAPDIAG flag={name} out_stride={} kernel_len={} reference_len={} max_gap={max} over3={over_three} x={x} y={y} channel={channel} kernel={} reference={}",
        converted.stride(0),
        kernel.len(),
        reference.len(),
        kernel[at],
        reference[at],
    );
}
