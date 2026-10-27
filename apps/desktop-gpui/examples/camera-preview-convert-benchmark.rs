//! Camera-preview conversion benchmark: the shipped RenderImage path against
//! the IOSurface surface-paint path, head to head on synthetic 420v frames.
//!
//! "old" reproduces `frame::FrameConverter` in `src/camera_window.rs` exactly:
//! VideoToolbox transfers 420v -> BGRA into a reused CPU pixel buffer, then a
//! per-frame `vec![0; w*h*4]` + row-copy lifts it into a gpui `RenderImage`
//! (which the window then re-uploads through the sprite atlas -- not measured
//! here, so "old" numbers are a lower bound on the shipped cost).
//!
//! "new" is the replacement: the same VideoToolbox transfer, but into a
//! Metal-compatible IOSurface-backed BGRA buffer from a fixed ring, ready for
//! `paint_surface` with zero further CPU touches.
//!
//! Run from `apps/desktop-gpui` (macOS keeps the IOSurface comparison above;
//! Linux and Windows compare swscale with the BT.601 kernel):
//!   cargo run --release --example camera-preview-convert-benchmark
//!
//! From the repo root the same example is:
//!   cargo run --manifest-path apps/desktop-gpui/Cargo.toml --release --example camera-preview-convert-benchmark
//!
//! Windows: `node scripts/setup.js` first, then put `target\ffmpeg\bin` on `PATH`
//! so the example can load the shared FFmpeg the desktop app is built against.
//! The command is unchanged. On Linux and Windows the CSV reports median, p90,
//! and the spread of per-repetition medians for swscale into a reused frame,
//! swscale into a fresh frame, the old allocating preview (`Video::empty` +
//! `copy_rows`), a full write of a new frame, `copy_rows`, and the kernel.
//! No display and no GPU.

#[cfg(not(target_os = "macos"))]
fn main() {
    portable::run();
}

#[cfg(target_os = "macos")]
fn main() {
    macos::run();
}

#[cfg(target_os = "macos")]
mod macos {
    use std::sync::Arc;
    use std::time::Instant;

    use cidre::{arc, cf, cv, vt};
    use gpui::RenderImage;
    use image::Frame;
    use smallvec::smallvec;

    unsafe extern "C-unwind" {
        fn CVPixelBufferGetBaseAddress(pixel_buf: *const cv::PixelBuf) -> *const u8;
        fn CVPixelBufferGetBytesPerRow(pixel_buf: *const cv::PixelBuf) -> usize;
    }

    const WARMUP: usize = 30;
    const ITERATIONS: usize = 300;
    const SOURCE_RING: usize = 4;
    const DEST_RING: usize = 4;

    struct Stats {
        samples: Vec<f64>,
    }

    impl Stats {
        fn new() -> Self {
            Self {
                samples: Vec::with_capacity(ITERATIONS),
            }
        }

        fn push(&mut self, us: f64) {
            self.samples.push(us);
        }

        fn report(&mut self) -> (f64, f64, f64) {
            self.samples
                .sort_by(|a, b| a.partial_cmp(b).expect("no NaN samples"));
            let mean = self.samples.iter().sum::<f64>() / self.samples.len() as f64;
            let p50 = self.samples[self.samples.len() / 2];
            let p95 = self.samples[(self.samples.len() as f64 * 0.95) as usize];
            (mean, p50, p95)
        }
    }

    fn iosurface_bgra_attrs() -> arc::R<cf::Dictionary> {
        let io_surface_properties = cf::Dictionary::new();
        let keys: [&cf::Type; 2] = [
            cv::pixel_buffer::keys::io_surf_props().as_ref(),
            cv::pixel_buffer::keys::metal_compatibility().as_ref(),
        ];
        let values: [&cf::Type; 2] = [
            io_surface_properties.as_ref(),
            cf::Boolean::value_true().as_ref(),
        ];
        cf::Dictionary::with_keys_values(&keys, &values).expect("pixel buffer attributes")
    }

    fn make_420v_source(width: usize, height: usize, seed: u8) -> arc::R<cv::PixelBuf> {
        let attrs = iosurface_bgra_attrs();
        let mut buf = cv::PixelBuf::new(width, height, cv::PixelFormat::_420V, Some(&attrs))
            .expect("420v source buffer");
        unsafe {
            buf.lock_base_addr(cv::pixel_buffer::LockFlags::DEFAULT)
                .result()
                .expect("lock 420v source");
        }
        let y_stride = buf.plane_bytes_per_row(0);
        let y_height = buf.plane_height(0);
        let uv_stride = buf.plane_bytes_per_row(1);
        let uv_height = buf.plane_height(1);
        unsafe {
            let y_base = buf.plane_base_address(0).cast_mut();
            for row in 0..y_height {
                for col in 0..width {
                    // Video-range luma with structure in both axes.
                    let value = 16 + ((row * 7 + col * 3 + seed as usize * 31) % 220) as u8;
                    *y_base.add(row * y_stride + col) = value;
                }
            }
            let uv_base = buf.plane_base_address(1).cast_mut();
            for row in 0..uv_height {
                for pair in 0..(width / 2) {
                    let cb = 16 + ((row * 5 + pair * 2 + seed as usize * 17) % 208) as u8;
                    let cr = 16 + ((row * 3 + pair * 5 + seed as usize * 23) % 208) as u8;
                    *uv_base.add(row * uv_stride + pair * 2) = cb;
                    *uv_base.add(row * uv_stride + pair * 2 + 1) = cr;
                }
            }
            buf.unlock_lock_base_addr(cv::pixel_buffer::LockFlags::DEFAULT);
        }
        buf
    }

    fn old_path_frame(
        session: &vt::PixelTransferSession,
        src: &cv::PixelBuf,
        dst: &mut arc::R<cv::PixelBuf>,
        width: usize,
        height: usize,
    ) -> Option<Arc<RenderImage>> {
        session.transfer(src, dst).ok()?;
        let mut data = vec![0u8; width * height * 4];
        {
            let dst_ptr = dst.as_ref() as *const cv::PixelBuf;
            let _guard = dst
                .base_address_lock(cv::pixel_buffer::LockFlags::READ_ONLY)
                .ok()?;
            let base = unsafe { CVPixelBufferGetBaseAddress(dst_ptr) };
            let stride = unsafe { CVPixelBufferGetBytesPerRow(dst_ptr) };
            if base.is_null() || stride < width * 4 {
                return None;
            }
            for row in 0..height {
                let src_row =
                    unsafe { std::slice::from_raw_parts(base.add(row * stride), width * 4) };
                data[row * width * 4..(row + 1) * width * 4].copy_from_slice(src_row);
            }
        }
        let buffer = image::RgbaImage::from_raw(width as u32, height as u32, data)?;
        Some(Arc::new(RenderImage::new(smallvec![Frame::new(buffer)])))
    }

    fn read_bgra(buf: &mut arc::R<cv::PixelBuf>, width: usize, height: usize) -> Vec<u8> {
        let mut data = vec![0u8; width * height * 4];
        let ptr = buf.as_ref() as *const cv::PixelBuf;
        let _guard = buf
            .base_address_lock(cv::pixel_buffer::LockFlags::READ_ONLY)
            .expect("lock BGRA destination");
        let base = unsafe { CVPixelBufferGetBaseAddress(ptr) };
        let stride = unsafe { CVPixelBufferGetBytesPerRow(ptr) };
        assert!(!base.is_null() && stride >= width * 4);
        for row in 0..height {
            let src_row = unsafe { std::slice::from_raw_parts(base.add(row * stride), width * 4) };
            data[row * width * 4..(row + 1) * width * 4].copy_from_slice(src_row);
        }
        data
    }

    pub fn run() {
        let resolutions = [(1280usize, 720usize), (1920, 1080), (3840, 2160)];
        println!(" resolution |    path | mean / p50 / p95 (us)");

        for (width, height) in resolutions {
            let sources: Vec<_> = (0..SOURCE_RING)
                .map(|seed| make_420v_source(width, height, seed as u8))
                .collect();

            let mut session = vt::PixelTransferSession::new().expect("VT session");
            session.set_realtime(true).expect("VT realtime");

            let mut cpu_dst = cv::PixelBuf::new(width, height, cv::PixelFormat::_32_BGRA, None)
                .expect("CPU BGRA destination");

            let attrs = iosurface_bgra_attrs();
            let mut surface_ring: Vec<arc::R<cv::PixelBuf>> = (0..DEST_RING)
                .map(|_| {
                    cv::PixelBuf::new(width, height, cv::PixelFormat::_32_BGRA, Some(&attrs))
                        .expect("IOSurface BGRA destination")
                })
                .collect();
            for slot in &surface_ring {
                assert!(
                    slot.io_surf().is_some(),
                    "destination ring must be IOSurface-backed"
                );
            }

            let mut old_stats = Stats::new();
            let mut new_stats = Stats::new();
            let mut next_slot = 0usize;

            for iteration in 0..(WARMUP + ITERATIONS) {
                let src = &sources[iteration % SOURCE_RING];

                let started = Instant::now();
                let image = old_path_frame(&session, src, &mut cpu_dst, width, height)
                    .expect("old path frame");
                let old_us = started.elapsed().as_secs_f64() * 1e6;
                drop(image);

                let started = Instant::now();
                session
                    .transfer(src, &surface_ring[next_slot])
                    .expect("transfer to IOSurface");
                let new_us = started.elapsed().as_secs_f64() * 1e6;
                next_slot = (next_slot + 1) % DEST_RING;

                if iteration >= WARMUP {
                    old_stats.push(old_us);
                    new_stats.push(new_us);
                }
            }

            let (mean, p50, p95) = old_stats.report();
            println!("{width}x{height:<6} |     old | {mean:8.1} / {p50:8.1} / {p95:8.1}");
            let (mean, p50, p95) = new_stats.report();
            println!("{width}x{height:<6} |     new | {mean:8.1} / {p50:8.1} / {p95:8.1}");

            session
                .transfer(&sources[0], &cpu_dst)
                .expect("exactness transfer to CPU");
            session
                .transfer(&sources[0], &surface_ring[0])
                .expect("exactness transfer to IOSurface");
            let cpu_pixels = read_bgra(&mut cpu_dst, width, height);
            let surface_pixels = read_bgra(&mut surface_ring[0], width, height);
            let mut max_delta = 0u8;
            let mut differing = 0usize;
            for (a, b) in cpu_pixels.iter().zip(surface_pixels.iter()) {
                let delta = a.abs_diff(*b);
                if delta > 0 {
                    differing += 1;
                }
                max_delta = max_delta.max(delta);
            }
            println!(
                "{width}x{height:<6} | exactness: max channel delta {max_delta}, {differing}/{} bytes differ",
                cpu_pixels.len()
            );
            assert!(
                max_delta <= 2,
                "IOSurface destination diverges from the CPU destination"
            );
        }
    }
}

/// Linux and Windows: swscale against the BT.601 kernel, each timed alone.
///
/// `sws_convert` is `scaler.run` into one reused BGRA frame. `sws_fresh` is the
/// same run into a new `Video::empty` frame, dropped without `copy_rows`.
/// `sws_app` is what `camera_preview_image` used to do: `Video::empty` plus
/// `copy_rows` every frame. `touch` writes every byte of a new BGRA frame and
/// drops it. `copy_rows` copies the reused frame into a new `Vec`. The kernel
/// column allocates, converts, and drops, which is the steady state once the
/// previous preview image has been released. The `RenderImage` owns that
/// buffer, so this loop cannot hand it back. At 1080p `sws_app` faults about
/// four thousand pages per frame — the new AVFrame and the tight `Vec` are
/// both cold — and a kernel sample run straight after that is about 2.5–3 ms.
/// Timed alone, the same kernel stays near 1 ms. 720p does not fault.
/// `max_abs_diff` is the largest channel gap. On Linux a `# faults` line is
/// minor faults per call after a short warmup, plus that follow-on kernel time.
#[cfg(not(target_os = "macos"))]
mod portable {
    use std::{hint::black_box, time::Instant};

    use cap_gpui_kernels::{
        camera_bgra::{self, Packed422},
        frame,
    };
    use ffmpeg::{
        format::Pixel,
        software::scaling::{Context, Flags},
    };

    const REPS: usize = 7;
    const WARMUP: usize = 12;
    const SAMPLES: usize = 40;

    pub fn run() {
        ffmpeg::init().expect("ffmpeg");
        // Untimed, so the first measured size is not still leaving idle.
        burn_in();
        println!(
            "case,width,height,reps,sws_convert_median_ms,sws_convert_p90_ms,sws_convert_spread_ms,sws_fresh_median_ms,sws_fresh_p90_ms,sws_app_median_ms,sws_app_p90_ms,sws_app_spread_ms,touch_median_ms,touch_p90_ms,copy_rows_median_ms,copy_rows_p90_ms,kernel_median_ms,kernel_p90_ms,kernel_spread_ms,speedup_vs_convert,speedup_vs_app,max_abs_diff"
        );
        println!(
            "# each column is timed alone ({REPS} reps x {SAMPLES} frames after {WARMUP} warmup). spread_ms is max(rep median) - min(rep median). sws_convert reuses one BGRA frame. sws_fresh is Video::empty + run + drop. sws_app is Video::empty + run + copy_rows. touch fills a new BGRA frame. copy_rows copies the reused frame into a new Vec. kernel is packed422_to_bgra / nv12_to_bgra."
        );
        for (name, pixel, width, height) in [
            ("YUYV", Pixel::YUYV422, 1280, 720),
            ("YUYV", Pixel::YUYV422, 1920, 1080),
            ("UYVY", Pixel::UYVY422, 1280, 720),
            ("UYVY", Pixel::UYVY422, 1920, 1080),
            ("NV12", Pixel::NV12, 1280, 720),
            ("NV12", Pixel::NV12, 1920, 1080),
        ] {
            bench(name, pixel, width, height);
        }
    }

    fn burn_in() {
        let mut frame = ffmpeg::frame::Video::new(Pixel::YUYV422, 1920, 1080);
        fill(&mut frame);
        let mut scaler = Context::get(
            Pixel::YUYV422,
            1920,
            1080,
            Pixel::BGRA,
            1920,
            1080,
            Flags::BILINEAR,
        )
        .expect("swscale context");
        for _ in 0..20 {
            black_box(swscale_app(&mut scaler, &frame));
            black_box(kernel_bgra(&frame));
        }
    }

    fn bench(name: &str, pixel: Pixel, width: u32, height: u32) {
        let mut frame = ffmpeg::frame::Video::new(pixel, width, height);
        fill(&mut frame);
        let mut scaler = Context::get(
            pixel,
            width,
            height,
            Pixel::BGRA,
            width,
            height,
            Flags::BILINEAR,
        )
        .expect("swscale context");
        let mut reused = ffmpeg::frame::Video::new(Pixel::BGRA, width, height);
        scaler.run(&frame, &mut reused).expect("swscale");
        let swscale_frame = copy_tight(&reused);
        let kernel_frame = kernel_bgra(&frame);
        assert_eq!(swscale_frame.len(), kernel_frame.len());
        let max_abs_diff = swscale_frame
            .iter()
            .zip(&kernel_frame)
            .map(|(before, after)| before.abs_diff(*after))
            .max()
            .unwrap_or(0);

        let convert_stats = time_path(|| {
            scaler.run(&frame, &mut reused).expect("swscale");
            black_box(reused.data(0)[0]);
        });
        let fresh_stats = time_path(|| {
            swscale_fresh(&mut scaler, &frame);
        });
        let app_stats = time_path(|| {
            black_box(swscale_app(&mut scaler, &frame));
        });
        let touch_stats = time_path(|| {
            touch_fresh_bgra(width, height);
        });
        let copy_stats = time_path(|| {
            black_box(copy_tight(&reused));
        });
        let kernel_stats = time_path(|| {
            black_box(kernel_bgra(&frame));
        });
        println!(
            "{name},{width},{height},{REPS},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.2},{:.2},{max_abs_diff}",
            convert_stats.median,
            convert_stats.p90,
            convert_stats.spread,
            fresh_stats.median,
            fresh_stats.p90,
            app_stats.median,
            app_stats.p90,
            app_stats.spread,
            touch_stats.median,
            touch_stats.p90,
            copy_stats.median,
            copy_stats.p90,
            kernel_stats.median,
            kernel_stats.p90,
            kernel_stats.spread,
            convert_stats.median / kernel_stats.median,
            app_stats.median / kernel_stats.median,
        );
        print_faults(name, width, height, &mut scaler, &frame, &mut reused);
    }

    struct PathStats {
        median: f64,
        p90: f64,
        spread: f64,
    }

    fn time_path(mut body: impl FnMut()) -> PathStats {
        let mut pooled = Vec::with_capacity(REPS * SAMPLES);
        let mut rep_medians = Vec::with_capacity(REPS);
        for _ in 0..REPS {
            for _ in 0..WARMUP {
                body();
            }
            let mut rep = Vec::with_capacity(SAMPLES);
            for _ in 0..SAMPLES {
                rep.push(elapsed_ms(&mut body));
            }
            pooled.extend_from_slice(&rep);
            rep_medians.push(percentile(&mut rep, 50));
        }
        let median = percentile(&mut pooled, 50);
        let p90 = percentile(&mut pooled, 90);
        rep_medians.sort_by(f64::total_cmp);
        let spread = rep_medians[rep_medians.len() - 1] - rep_medians[0];
        PathStats {
            median,
            p90,
            spread,
        }
    }

    fn elapsed_ms(body: impl FnOnce()) -> f64 {
        let started = Instant::now();
        body();
        started.elapsed().as_secs_f64() * 1_000.0
    }

    fn percentile(samples: &mut [f64], pct: usize) -> f64 {
        samples.sort_by(f64::total_cmp);
        let index = (samples.len() - 1) * pct / 100;
        samples[index]
    }

    fn fill(frame: &mut ffmpeg::frame::Video) {
        let planes = if frame.format() == Pixel::NV12 { 2 } else { 1 };
        for plane in 0..planes {
            for (index, byte) in frame.data_mut(plane).iter_mut().enumerate() {
                *byte = index.wrapping_mul(17).wrapping_add(plane * 41) as u8;
            }
        }
    }

    fn copy_tight(frame: &ffmpeg::frame::Video) -> Vec<u8> {
        frame::copy_rows(
            frame.data(0),
            frame.width() as usize * 4,
            frame.stride(0),
            frame.height() as usize,
            false,
        )
        .expect("tight bgra rows")
    }

    fn swscale_fresh(scaler: &mut Context, frame: &ffmpeg::frame::Video) {
        let mut converted = ffmpeg::frame::Video::empty();
        scaler.run(frame, &mut converted).expect("swscale");
        black_box(converted.data(0)[0]);
    }

    fn swscale_app(scaler: &mut Context, frame: &ffmpeg::frame::Video) -> Vec<u8> {
        let mut converted = ffmpeg::frame::Video::empty();
        scaler.run(frame, &mut converted).expect("swscale");
        copy_tight(&converted)
    }

    fn touch_fresh_bgra(width: u32, height: u32) {
        let mut frame = ffmpeg::frame::Video::new(Pixel::BGRA, width, height);
        frame.data_mut(0).fill(0);
        black_box(frame.data(0)[0]);
    }

    fn print_faults(
        name: &str,
        width: u32,
        height: u32,
        scaler: &mut Context,
        frame: &ffmpeg::frame::Video,
        reused: &mut ffmpeg::frame::Video,
    ) {
        #[cfg(target_os = "linux")]
        {
            let convert = faults_per_call(|| {
                scaler.run(frame, reused).expect("swscale");
                black_box(reused.data(0)[0]);
            });
            let fresh = faults_per_call(|| swscale_fresh(scaler, frame));
            let touch = faults_per_call(|| touch_fresh_bgra(width, height));
            let copy = faults_per_call(|| {
                black_box(copy_tight(reused));
            });
            let kernel = faults_per_call(|| {
                black_box(kernel_bgra(frame));
            });
            let app = faults_per_call(|| {
                black_box(swscale_app(scaler, frame));
            });
            let (run_ms, copy_ms, drop_ms) = stage_ms(scaler, frame);
            let alone = kernel_after(scaler, frame, reused, Interleave::Alone);
            let after_convert = kernel_after(scaler, frame, reused, Interleave::Convert);
            let after_app = kernel_after(scaler, frame, reused, Interleave::App);
            println!(
                "# faults {name} {width}x{height} minor/call convert={convert} fresh={fresh} touch={touch} copy_rows={copy} kernel={kernel} app={app} | inside_app_ms run={run_ms:.3} copy={copy_ms:.3} drop={drop_ms:.3} | kernel_ms alone={alone:.3} after_convert={after_convert:.3} after_app={after_app:.3}"
            );
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (name, width, height, scaler, frame, reused);
        }
    }

    #[cfg(target_os = "linux")]
    fn faults_per_call(mut body: impl FnMut()) -> u64 {
        for _ in 0..4 {
            body();
        }
        let before = minor_faults();
        for _ in 0..8 {
            body();
        }
        minor_faults().saturating_sub(before) / 8
    }

    #[cfg(target_os = "linux")]
    enum Interleave {
        Alone,
        Convert,
        App,
    }

    #[cfg(target_os = "linux")]
    fn kernel_after(
        scaler: &mut Context,
        frame: &ffmpeg::frame::Video,
        reused: &mut ffmpeg::frame::Video,
        kind: Interleave,
    ) -> f64 {
        let mut samples = Vec::with_capacity(20);
        for _ in 0..6 {
            match kind {
                Interleave::Alone => {}
                Interleave::Convert => {
                    scaler.run(frame, reused).expect("swscale");
                }
                Interleave::App => {
                    swscale_app(scaler, frame);
                }
            }
            black_box(kernel_bgra(frame));
        }
        for _ in 0..20 {
            match kind {
                Interleave::Alone => {}
                Interleave::Convert => {
                    scaler.run(frame, reused).expect("swscale");
                }
                Interleave::App => {
                    swscale_app(scaler, frame);
                }
            }
            samples.push(elapsed_ms(|| {
                black_box(kernel_bgra(frame));
            }));
        }
        percentile(&mut samples, 50)
    }

    #[cfg(target_os = "linux")]
    fn stage_ms(scaler: &mut Context, frame: &ffmpeg::frame::Video) -> (f64, f64, f64) {
        let mut run_ms = Vec::with_capacity(20);
        let mut copy_ms = Vec::with_capacity(20);
        let mut drop_ms = Vec::with_capacity(20);
        for _ in 0..8 {
            swscale_app(scaler, frame);
        }
        for _ in 0..20 {
            let mut converted = ffmpeg::frame::Video::empty();
            let started = Instant::now();
            scaler.run(frame, &mut converted).expect("swscale");
            run_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
            let started = Instant::now();
            let pixels = copy_tight(&converted);
            copy_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
            let started = Instant::now();
            drop(pixels);
            drop(converted);
            drop_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
        }
        (
            percentile(&mut run_ms, 50),
            percentile(&mut copy_ms, 50),
            percentile(&mut drop_ms, 50),
        )
    }

    #[cfg(target_os = "linux")]
    fn minor_faults() -> u64 {
        let stat = std::fs::read_to_string("/proc/self/stat").expect("proc stat");
        let rest = stat.rsplit_once(')').expect("comm").1;
        rest.split_whitespace()
            .nth(7)
            .expect("minflt")
            .parse()
            .expect("minflt number")
    }

    fn kernel_bgra(frame: &ffmpeg::frame::Video) -> Vec<u8> {
        let width = frame.width() as usize;
        let height = frame.height() as usize;
        match frame.format() {
            Pixel::YUYV422 => camera_bgra::packed422_to_bgra(
                frame.data(0),
                width,
                height,
                frame.stride(0),
                Packed422::Yuyv,
                false,
            ),
            Pixel::UYVY422 => camera_bgra::packed422_to_bgra(
                frame.data(0),
                width,
                height,
                frame.stride(0),
                Packed422::Uyvy,
                false,
            ),
            Pixel::NV12 => camera_bgra::nv12_to_bgra(
                frame.data(0),
                frame.data(1),
                width,
                height,
                frame.stride(0),
                frame.stride(1),
                false,
            ),
            _ => unreachable!("benchmark formats are YUYV, UYVY, and NV12"),
        }
        .expect("kernel conversion")
    }
}
