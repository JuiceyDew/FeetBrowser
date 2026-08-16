//! Software rasteriser: a pixel buffer and the operations that mark it.
//!
//! This is the drawing half of what used to be Tk. A Surface owns a flat RGB
//! buffer -- three bytes a pixel, no padding -- plus a clip rectangle, and
//! every mark the browser makes goes through one of the methods here.
//!
//! Two things shape the code. The first is that the buffer stays on this side
//! of the boundary: it is allocated once, handed to Python only as a
//! memoryview, and never resized, so a paint is a sequence of cheap calls
//! rather than a bytearray shuttled back and forth. The second is that every
//! coordinate arrives from layout, which got it from a stylesheet, which got
//! it from a page: nothing here may index on a number a page chose. Rectangles
//! are clipped in i64 before anything becomes an offset, and each offset is
//! range-checked once per row rather than trusted.

use crate::font::{flatten_contours, Font};
use crate::pyutil::{bytes_arg, for_each_char, rgb, to_int};
use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyMemoryView, PyTuple};
use std::os::raw::c_int;

/// An RGB pixel buffer with a clip rectangle.
#[pyclass(module = "feetbrowser_engine")]
pub struct Surface {
    #[pyo3(get)]
    width: i64,
    #[pyo3(get)]
    height: i64,
    #[pyo3(get)]
    stride: i64,
    px: Vec<u8>,
    clip: (i64, i64, i64, i64),
}

/// Clamp a rectangle to the clip and return it as byte offsets, or None when
/// nothing is left of it. Every drawing method starts here, which is what
/// keeps the indexing below honest.
fn clipped(
    surface: &Surface,
    x0: i64,
    y0: i64,
    x1: i64,
    y1: i64,
) -> Option<(i64, i64, i64, i64)> {
    let (cx0, cy0, cx1, cy1) = surface.clip;
    let x0 = x0.max(cx0);
    let y0 = y0.max(cy0);
    let x1 = x1.min(cx1);
    let y1 = y1.min(cy1);
    if x0 >= x1 || y0 >= y1 {
        return None;
    }
    Some((x0, y0, x1, y1))
}

#[inline]
fn blend(dst: u8, src: u8, alpha: i64, inv: i64) -> u8 {
    // Integer arithmetic on purpose: this is the expression the Python
    // renderer used, floor division and all, and rounding it differently
    // would move every anti-aliased edge in the browser by a shade.
    ((dst as i64 * inv + src as i64 * alpha) / 255) as u8
}

#[pymethods]
impl Surface {
    #[new]
    #[pyo3(signature = (width, height, background = None))]
    fn new(
        width: &Bound<'_, PyAny>,
        height: &Bound<'_, PyAny>,
        background: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let width = to_int(width)?.max(1);
        let height = to_int(height)?.max(1);
        let stride = width
            .checked_mul(3)
            .ok_or_else(|| PyMemoryError::new_err("surface too wide"))?;
        let bytes = stride
            .checked_mul(height)
            .and_then(|n| usize::try_from(n).ok())
            .ok_or_else(|| PyMemoryError::new_err("surface too large"))?;
        let colour = match background {
            Some(obj) => rgb(obj)?,
            None => [255, 255, 255],
        };
        let mut px = Vec::new();
        px.try_reserve_exact(bytes)
            .map_err(|_| PyMemoryError::new_err("surface too large"))?;
        px.resize(bytes, 0);
        for p in px.chunks_exact_mut(3) {
            p[0] = colour[0];
            p[1] = colour[1];
            p[2] = colour[2];
        }
        Ok(Surface {
            width,
            height,
            stride,
            px,
            clip: (0, 0, width, height),
        })
    }

    #[getter]
    fn clip(&self) -> (i64, i64, i64, i64) {
        self.clip
    }

    /// The framebuffer, as a memoryview onto the buffer Rust owns.
    ///
    /// Callers read it whole (the window backend blits it, tests inspect
    /// single pixels), and a memoryview lets them do that without copying a
    /// megabyte per frame. It is read-only: everything that writes pixels is
    /// a method on this class.
    #[getter]
    fn pixels<'py>(slf: Bound<'py, Self>) -> PyResult<Bound<'py, PyMemoryView>> {
        PyMemoryView::from(&slf)
    }

    unsafe fn __getbuffer__(
        slf: PyRefMut<'_, Self>,
        view: *mut ffi::Py_buffer,
        flags: c_int,
    ) -> PyResult<()> {
        // The buffer is allocated once in `new` and never grows, so the
        // pointer we hand out stays valid for as long as the surface does.
        unsafe {
            ffi::PyBuffer_FillInfo(
                view,
                slf.as_ptr(),
                slf.px.as_ptr() as *mut std::ffi::c_void,
                slf.px.len() as ffi::Py_ssize_t,
                1, // read-only
                flags,
            );
        }
        Ok(())
    }

    unsafe fn __releasebuffer__(&self, _view: *mut ffi::Py_buffer) {}

    // -- clipping --------------------------------------------------------

    /// Restrict drawing to a rectangle; returns the previous clip.
    fn set_clip(
        &mut self,
        x0: &Bound<'_, PyAny>,
        y0: &Bound<'_, PyAny>,
        x1: &Bound<'_, PyAny>,
        y1: &Bound<'_, PyAny>,
    ) -> PyResult<(i64, i64, i64, i64)> {
        let old = self.clip;
        self.clip = (
            to_int(x0)?.max(0),
            to_int(y0)?.max(0),
            to_int(x1)?.min(self.width),
            to_int(y1)?.min(self.height),
        );
        Ok(old)
    }

    #[pyo3(signature = (saved = None))]
    fn reset_clip(&mut self, saved: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        // Python treated an empty tuple as "no saved clip" by testing the
        // value for truth, and canvas.py relies on passing None.
        self.clip = match saved {
            Some(obj) if obj.is_truthy()? => {
                let t: (i64, i64, i64, i64) = obj.extract()?;
                t
            }
            _ => (0, 0, self.width, self.height),
        };
        Ok(())
    }

    // -- fills -----------------------------------------------------------

    fn fill_all(&mut self, color: &Bound<'_, PyAny>) -> PyResult<()> {
        let c = rgb(color)?;
        for p in self.px.chunks_exact_mut(3) {
            p[0] = c[0];
            p[1] = c[1];
            p[2] = c[2];
        }
        Ok(())
    }

    /// Axis-aligned fill, opaque or blended.
    #[pyo3(signature = (x0, y0, x1, y1, color, alpha = None))]
    fn fill_rect(
        &mut self,
        x0: &Bound<'_, PyAny>,
        y0: &Bound<'_, PyAny>,
        x1: &Bound<'_, PyAny>,
        y1: &Bound<'_, PyAny>,
        color: &Bound<'_, PyAny>,
        alpha: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let alpha = match alpha {
            Some(a) => to_int(a)?,
            None => 255,
        };
        let (x0, y0, x1, y1) = (to_int(x0)?, to_int(y0)?, to_int(x1)?, to_int(y1)?);
        if alpha <= 0 || clipped(self, x0, y0, x1, y1).is_none() {
            return Ok(());
        }
        let c = rgb(color)?;
        self.fill_span(x0, y0, x1, y1, c, alpha);
        Ok(())
    }

    #[pyo3(signature = (x0, y0, x1, y1, color, thickness = None, alpha = None))]
    fn outline_rect(
        &mut self,
        x0: &Bound<'_, PyAny>,
        y0: &Bound<'_, PyAny>,
        x1: &Bound<'_, PyAny>,
        y1: &Bound<'_, PyAny>,
        color: &Bound<'_, PyAny>,
        thickness: Option<&Bound<'_, PyAny>>,
        alpha: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let alpha = match alpha {
            Some(a) => to_int(a)?,
            None => 255,
        };
        let t = match thickness {
            Some(v) => to_int(v)?.max(1),
            None => 1,
        };
        // The four sides are laid out exactly as the Python was: top and
        // bottom run the full width, the two uprights fit between them.
        let (ax0, ay0, ax1, ay1) = (to_int(x0)?, to_int(y0)?, to_int(x1)?, to_int(y1)?);
        let c = rgb(color)?;
        self.fill_span(ax0, ay0, ax1, ay0.saturating_add(t), c, alpha);
        self.fill_span(ax0, ay1.saturating_sub(t), ax1, ay1, c, alpha);
        self.fill_span(ax0, ay0.saturating_add(t), ax0.saturating_add(t),
                       ay1.saturating_sub(t), c, alpha);
        self.fill_span(ax1.saturating_sub(t), ay0.saturating_add(t), ax1,
                       ay1.saturating_sub(t), c, alpha);
        Ok(())
    }

    /// Straight line. Axis-aligned cases become fills; the rest steps.
    #[pyo3(signature = (x0, y0, x1, y1, color, thickness = None, alpha = None))]
    fn draw_line(
        &mut self,
        x0: &Bound<'_, PyAny>,
        y0: &Bound<'_, PyAny>,
        x1: &Bound<'_, PyAny>,
        y1: &Bound<'_, PyAny>,
        color: &Bound<'_, PyAny>,
        thickness: Option<&Bound<'_, PyAny>>,
        alpha: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let alpha = match alpha {
            Some(a) => to_int(a)?,
            None => 255,
        };
        let t = match thickness {
            Some(v) => to_int(v)?.max(1),
            None => 1,
        };
        let c = rgb(color)?;
        let (mut x0, mut y0) = (to_int(x0)?, to_int(y0)?);
        let (x1, y1) = (to_int(x1)?, to_int(y1)?);
        if y0 == y1 {
            self.fill_span(x0.min(x1), y0, x0.max(x1).saturating_add(1),
                           y0.saturating_add(t), c, alpha);
            return Ok(());
        }
        if x0 == x1 {
            self.fill_span(x0, y0.min(y1), x0.saturating_add(t),
                           y0.max(y1).saturating_add(1), c, alpha);
            return Ok(());
        }
        // Bresenham, with the step counted rather than trusted: the loop
        // below cannot run longer than the line is long, whatever arithmetic
        // a page's coordinates lead to.
        let dx = (x1 - x0).saturating_abs();
        let dy = (y1 - y0).saturating_abs();
        let sx: i64 = if x0 < x1 { 1 } else { -1 };
        let sy: i64 = if y0 < y1 { 1 } else { -1 };
        let mut err = dx - dy;
        let mut budget = dx.saturating_add(dy).saturating_add(1);
        loop {
            self.fill_span(x0, y0, x0.saturating_add(t), y0.saturating_add(t),
                           c, alpha);
            if (x0 == x1 && y0 == y1) || budget <= 0 {
                break;
            }
            budget -= 1;
            let e2 = err.saturating_mul(2);
            if e2 > -dy {
                err -= dy;
                x0 += sx;
            }
            if e2 < dx {
                err += dx;
                y0 += sy;
            }
        }
        Ok(())
    }

    // -- coverage compositing --------------------------------------------

    /// Composite an 8-bit coverage bitmap in a solid colour.
    fn blit_coverage(
        &mut self,
        cov: &Bound<'_, PyAny>,
        cw: &Bound<'_, PyAny>,
        ch: &Bound<'_, PyAny>,
        x: &Bound<'_, PyAny>,
        y: &Bound<'_, PyAny>,
        color: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let cw = to_int(cw)?;
        let ch = to_int(ch)?;
        if cw <= 0 || ch <= 0 {
            return Ok(());
        }
        let cov = bytes_arg(cov)?;
        let c = rgb(color)?;
        let (x, y) = (to_int(x)?, to_int(y)?);
        self.blit_cov(&cov, cw, ch, x, y, c);
        Ok(())
    }

    /// Composite raw RGBA bytes.
    ///
    /// `opaque` promises every alpha byte is 255, which turns the inner loop
    /// into a strided copy with no arithmetic at all -- the difference
    /// between a photo costing microseconds and costing milliseconds.
    ///
    /// `dw`/`dh` are the size to draw at, in destination pixels, and default
    /// to the source size. They are here for HiDPI: an image is the one thing
    /// on the canvas whose pixels are its own rather than ours, so a 100x100
    /// picture covering 100 CSS pixels has to cover 200 device pixels on a 2x
    /// display. Resampling on the way in, rather than rewriting the caller's
    /// buffer, is what keeps that from needing a cache that goes stale --
    /// animated GIFs and video rewrite their pixels in place and would
    /// silently outrun one. The sampling is nearest-neighbour, matching
    /// `image.resize`, so a picture scaled by CSS and one scaled by the
    /// display land on the same pixels rather than on two different ideas of
    /// where a source pixel went.
    #[pyo3(signature = (data, iw, ih, x, y, opaque = false, dw = None,
                        dh = None))]
    fn blit_rgba(
        &mut self,
        data: &Bound<'_, PyAny>,
        iw: &Bound<'_, PyAny>,
        ih: &Bound<'_, PyAny>,
        x: &Bound<'_, PyAny>,
        y: &Bound<'_, PyAny>,
        opaque: bool,
        dw: Option<&Bound<'_, PyAny>>,
        dh: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let iw = to_int(iw)?;
        let ih = to_int(ih)?;
        if iw <= 0 || ih <= 0 {
            return Ok(());
        }
        let dw = match dw {
            Some(v) => to_int(v)?,
            None => iw,
        };
        let dh = match dh {
            Some(v) => to_int(v)?,
            None => ih,
        };
        if dw <= 0 || dh <= 0 {
            return Ok(());
        }
        let data = bytes_arg(data)?;
        let (x, y) = (to_int(x)?, to_int(y)?);
        let (cx0, cy0, cx1, cy1) = self.clip;
        let sx0 = 0.max(cx0 - x);
        let sy0 = 0.max(cy0 - y);
        let sx1 = dw.min(cx1 - x);
        let sy1 = dh.min(cy1 - y);
        if sx0 >= sx1 || sy0 >= sy1 {
            return Ok(());
        }
        let count = (sx1 - sx0) as usize;
        // Destination column to the byte offset of the source pixel it
        // samples, computed once because it is the same on every row -- and
        // the identity mapping when nothing is being scaled, which is the
        // case that has to stay cheap.
        let xmap: Vec<usize> = (sx0..sx1)
            .map(|c| ((iw - 1).min(c * iw / dw) * 4) as usize)
            .collect();
        for row in sy0..sy1 {
            let src = ((ih - 1).min(row * ih / dh) * iw * 4) as usize;
            let dst = ((y + row) * self.stride + (x + sx0) * 3) as usize;
            let line = match self.px.get_mut(dst..dst + count * 3) {
                Some(l) => l,
                None => continue,
            };
            // An image whose buffer is shorter than its declared size only
            // happens if something lied about it; the pixels we do not have
            // are left as they were rather than read. `xmap` never
            // decreases, so testing its last entry answers for the whole row
            // and the scan only runs for a row that really is short.
            let room = data.len().saturating_sub(src);
            let n = if xmap[count - 1] + 4 <= room {
                count
            } else {
                xmap.iter().position(|o| o + 4 > room).unwrap_or(count)
            };
            for col in 0..n {
                let s = src + xmap[col];
                let d = col * 3;
                let a = data[s + 3] as i64;
                if opaque || a >= 255 {
                    if !opaque && a == 0 {
                        continue;
                    }
                    line[d] = data[s];
                    line[d + 1] = data[s + 1];
                    line[d + 2] = data[s + 2];
                } else if a > 0 {
                    let inv = 255 - a;
                    line[d] = blend(line[d], data[s], a, inv);
                    line[d + 1] = blend(line[d + 1], data[s + 1], a, inv);
                    line[d + 2] = blend(line[d + 2], data[s + 2], a, inv);
                }
            }
        }
        Ok(())
    }

    // -- output ----------------------------------------------------------

    /// Encode as PNG bytes.
    fn to_png<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let stride = self.stride as usize;
        let mut raw = Vec::with_capacity((stride + 1) * self.height as usize);
        for y in 0..self.height as usize {
            raw.push(0); // filter type 0 (None)
            let o = y * stride;
            match self.px.get(o..o + stride) {
                Some(line) => raw.extend_from_slice(line),
                None => break,
            }
        }
        png_bytes(py, self.width, self.height, &raw)
    }

    fn save_png(&self, py: Python<'_>, path: &str) -> PyResult<()> {
        let data = self.to_png(py)?;
        std::fs::write(path, data.as_bytes())?;
        Ok(())
    }
}

impl Surface {
    /// The body of `fill_rect` once the arguments are plain numbers, so the
    /// methods built out of rectangles do not pay for conversion four times.
    fn fill_span(&mut self, x0: i64, y0: i64, x1: i64, y1: i64, c: [u8; 3],
                 alpha: i64) {
        if alpha <= 0 {
            return;
        }
        let (x0, y0, x1, y1) = match clipped(self, x0, y0, x1, y1) {
            Some(r) => r,
            None => return,
        };
        let span = ((x1 - x0) * 3) as usize;
        let inv = 255 - alpha.min(255);
        for y in y0..y1 {
            let o = (y * self.stride + x0 * 3) as usize;
            let row = match self.px.get_mut(o..o + span) {
                Some(r) => r,
                None => continue,
            };
            if alpha >= 255 {
                for p in row.chunks_exact_mut(3) {
                    p[0] = c[0];
                    p[1] = c[1];
                    p[2] = c[2];
                }
            } else {
                for p in row.chunks_exact_mut(3) {
                    p[0] = blend(p[0], c[0], alpha, inv);
                    p[1] = blend(p[1], c[1], alpha, inv);
                    p[2] = blend(p[2], c[2], alpha, inv);
                }
            }
        }
    }

    /// The body of `blit_coverage`, shared with the text drawing below so a
    /// glyph does not make the round trip through Python to reach it.
    fn blit_cov(&mut self, cov: &[u8], cw: i64, ch: i64, x: i64, y: i64,
                c: [u8; 3]) {
        if cw <= 0 || ch <= 0 {
            return;
        }
        let (cx0, cy0, cx1, cy1) = self.clip;
        let sx0 = 0.max(cx0 - x);
        let sy0 = 0.max(cy0 - y);
        let sx1 = cw.min(cx1 - x);
        let sy1 = ch.min(cy1 - y);
        if sx0 >= sx1 || sy0 >= sy1 {
            return;
        }
        let count = (sx1 - sx0) as usize;
        for row in sy0..sy1 {
            let src = (row * cw) as usize;
            let dst = ((y + row) * self.stride + (x + sx0) * 3) as usize;
            let line = match self.px.get_mut(dst..dst + count * 3) {
                Some(l) => l,
                None => continue,
            };
            for col in 0..count {
                // A coverage bitmap shorter than it claims is not something
                // our own rasteriser produces, but reading past it would be a
                // panic, so a missing byte simply means no coverage.
                let a = match cov.get(src + sx0 as usize + col) {
                    Some(&a) => a as i64,
                    None => continue,
                };
                if a == 0 {
                    continue;
                }
                let d = col * 3;
                if a >= 255 {
                    line[d] = c[0];
                    line[d + 1] = c[1];
                    line[d + 2] = c[2];
                } else {
                    let inv = 255 - a;
                    line[d] = blend(line[d], c[0], a, inv);
                    line[d + 1] = blend(line[d + 1], c[1], a, inv);
                    line[d + 2] = blend(line[d + 2], c[2], a, inv);
                }
            }
        }
    }
}

// -- outline rasterisation ------------------------------------------------

/// Vertical subsamples per pixel row. Horizontal coverage is computed
/// analytically, so four rows is enough to look smooth.
const SUBSAMPLES: usize = 4;

struct Edge {
    y0: f64,
    y1: f64,
    x0: f64,
    slope: f64,
    wind: i64,
}

/// Scan-convert polygons into an 8-bit coverage bitmap.
///
/// Nonzero winding, matching TrueType. Coverage is sampled at SUBSAMPLES rows
/// per pixel and computed analytically across each span, so edges get real
/// anti-aliasing rather than a hard threshold.
///
/// This is the innermost loop of the whole renderer: every glyph that is not
/// already in the cache comes through here, and so does every polygon a page
/// draws. The structure is unchanged from the Python -- same sample
/// positions, same accumulation in floating point, same truncation at the end
/// -- because the coverage values it produces are what the anti-aliased edges
/// of the browser's text are made of.
#[pyfunction]
#[pyo3(signature = (polys, width, height, offset_x = 0.0, offset_y = 0.0))]
pub fn rasterize<'py>(
    py: Python<'py>,
    polys: &Bound<'py, PyAny>,
    width: i64,
    height: i64,
    offset_x: f64,
    offset_y: f64,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut shapes: Vec<Vec<(f64, f64)>> = Vec::new();
    for poly in polys.try_iter()? {
        shapes.push(poly?.extract()?);
    }
    let cov = rasterize_polys(&shapes, width, height, offset_x, offset_y)?;
    Ok(PyBytes::new(py, &cov))
}

/// The rasteriser proper, on plain numbers.
pub fn rasterize_polys(
    polys: &[Vec<(f64, f64)>],
    width: i64,
    height: i64,
    offset_x: f64,
    offset_y: f64,
) -> PyResult<Vec<u8>> {
    let area = width.saturating_mul(height);
    if area < 0 {
        return Err(PyValueError::new_err("negative count"));
    }
    let size = usize::try_from(area)
        .map_err(|_| PyMemoryError::new_err("coverage bitmap too large"))?;
    let mut cov = vec![0u8; size];
    if width <= 0 || height <= 0 {
        return Ok(cov);
    }

    let mut edges: Vec<Edge> = Vec::new();
    for points in polys {
        let n = points.len();
        for i in 0..n {
            let (mut x0, mut y0) = points[i];
            let (mut x1, mut y1) = points[(i + 1) % n];
            x0 += offset_x;
            y0 += offset_y;
            x1 += offset_x;
            y1 += offset_y;
            if y0 == y1 {
                continue; // horizontal edges never cross a scanline
            }
            edges.push(Edge {
                y0,
                y1,
                x0,
                slope: (x1 - x0) / (y1 - y0),
                wind: if y1 > y0 { 1 } else { -1 },
            });
        }
    }
    if edges.is_empty() {
        return Ok(cov);
    }

    let mut lo = f64::INFINITY;
    let mut hi = f64::NEG_INFINITY;
    for e in &edges {
        lo = lo.min(e.y0.min(e.y1));
        hi = hi.max(e.y0.max(e.y1));
    }
    // `int()` truncates towards zero, and an outline that starts above the
    // bitmap has a negative top, so this is not the same as flooring.
    let top = trunc_i64(lo).max(0);
    let bottom = trunc_i64(hi).saturating_add(1).min(height);
    let step = 1.0 / SUBSAMPLES as f64;
    let unit = 255.0 / SUBSAMPLES as f64;
    let w = width as usize;

    let mut acc = vec![0.0f64; w];
    let mut xs: Vec<(f64, i64)> = Vec::new();
    for row in top..bottom {
        acc.iter_mut().for_each(|v| *v = 0.0);
        let mut hit = false;
        for k in 0..SUBSAMPLES {
            let sy = row as f64 + (k as f64 + 0.5) * step;
            xs.clear();
            for e in &edges {
                if (e.y0 <= sy && sy < e.y1) || (e.y1 <= sy && sy < e.y0) {
                    xs.push((e.x0 + (sy - e.y0) * e.slope, e.wind));
                }
            }
            if xs.len() < 2 {
                continue;
            }
            // Python sorted the (x, winding) pairs as tuples. total_cmp
            // keeps that order and also gives NaN somewhere to go, which a
            // page can produce with a degenerate polygon.
            xs.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
            let mut winding: i64 = 0;
            let mut span_start = 0.0f64;
            for &(x, wind) in xs.iter() {
                if winding == 0 {
                    span_start = x;
                }
                winding += wind;
                if winding == 0 && x > span_start {
                    add_span(&mut acc, span_start, x, unit, w);
                    hit = true;
                }
            }
        }
        if !hit {
            continue;
        }
        let base = row as usize * w;
        for (i, &v) in acc.iter().enumerate() {
            if v > 0.0 {
                cov[base + i] = if v >= 255.0 { 255 } else { v as u8 };
            }
        }
    }
    Ok(cov)
}

// -- text ------------------------------------------------------------------

/// The most glyph bitmaps one face will hold before it stops caching.
///
/// Cached glyphs are kept on the face that produced them, so the cache lives
/// and dies with it. Keying a shared cache on the font's address instead would
/// let a collected face hand its address -- and its glyph shapes -- to the
/// next one allocated there.
const GLYPH_CACHE_MAX: usize = 20000;

/// Coverage bitmap for one glyph: `(cov, w, h, left, top)`.
///
/// `left`/`top` are offsets from the pen position on the baseline to the
/// bitmap's top-left corner, so callers place it without re-reading the
/// outline. The tuple is cached and handed back as it is, which is what keeps
/// drawing a repeated character down to one blend per pixel.
#[pyfunction]
pub fn glyph_bitmap(
    py: Python<'_>,
    font: &Bound<'_, Font>,
    size: f64,
    gid: u32,
) -> PyResult<Py<PyTuple>> {
    // Two floats that are equal key the same entry, which is what Python's
    // tuple key did; a size arriving as 24 and as 24.0 is one cache line.
    let key = (size.to_bits(), gid);
    if let Some(hit) = font.borrow().bitmaps.get(&key) {
        return Ok(hit.clone_ref(py));
    }

    let polys = {
        let mut f = font.borrow_mut();
        let scale = f.scale_of(size);
        let contours = f.contours(gid, 0);
        flatten_contours(&contours, scale, 8)
    };

    let empty = || (PyBytes::new(py, &[]), 0i64, 0i64, 0i64, 0i64).into_pyobject(py);
    let result: Bound<'_, PyTuple> = if polys.is_empty() {
        empty()?
    } else {
        let mut lo_x = f64::INFINITY;
        let mut lo_y = f64::INFINITY;
        let mut hi_x = f64::NEG_INFINITY;
        let mut hi_y = f64::NEG_INFINITY;
        for poly in &polys {
            for &(x, y) in poly {
                lo_x = lo_x.min(x);
                lo_y = lo_y.min(y);
                hi_x = hi_x.max(x);
                hi_y = hi_y.max(y);
            }
        }
        let left = trunc_i64(lo_x) - 1;
        let top = trunc_i64(lo_y) - 1;
        let w = trunc_i64(hi_x) - left + 2;
        let h = trunc_i64(hi_y) - top + 2;
        // A glyph bigger than 4096 pixels a side is a font bug or a size no
        // page needs; drawing nothing beats allocating for it.
        if w <= 0 || h <= 0 || w > 4096 || h > 4096 {
            empty()?
        } else {
            let cov = rasterize_polys(&polys, w, h, -left as f64, -top as f64)?;
            (PyBytes::new(py, &cov), w, h, left, top).into_pyobject(py)?
        }
    };

    let result: Py<PyTuple> = result.unbind();
    let mut f = font.borrow_mut();
    if f.bitmaps.len() < GLYPH_CACHE_MAX {
        f.bitmaps.insert(key, result.clone_ref(py));
    }
    Ok(result)
}

/// Draw a string, returning the advance in pixels.
///
/// Advances are summed per character with no kerning, which keeps the layout
/// engine's per-character width cache exact.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn draw_text(
    py: Python<'_>,
    surface: &Bound<'_, Surface>,
    font: &Bound<'_, Font>,
    size: f64,
    text: &Bound<'_, PyAny>,
    x: f64,
    baseline: f64,
    color: &Bound<'_, PyAny>,
) -> PyResult<f64> {
    let c = rgb(color)?;
    let scale = font.borrow().scale_of(size);
    let mut pen = x;
    let mut failed: Option<PyErr> = None;
    for_each_char(text, |code| {
        if failed.is_some() {
            return;
        }
        let gid = font.borrow_mut().glyph_of(code);
        let adv = font.borrow().advance_of(gid as i64) as f64 * scale;
        // Space and tab advance the pen without a trip through the glyph
        // cache; they are most of the characters on a page.
        if code != 0x20 && code != 0x09 {
            if let Err(e) = blit_glyph(py, surface, font, size, gid, pen,
                                       baseline, c) {
                failed = Some(e);
                return;
            }
        }
        pen += adv;
    })?;
    match failed {
        Some(e) => Err(e),
        None => Ok(pen - x),
    }
}

fn blit_glyph(
    py: Python<'_>,
    surface: &Bound<'_, Surface>,
    font: &Bound<'_, Font>,
    size: f64,
    gid: u32,
    pen: f64,
    baseline: f64,
    c: [u8; 3],
) -> PyResult<()> {
    let bitmap = glyph_bitmap(py, font, size, gid)?;
    let bitmap = bitmap.bind(py);
    let w: i64 = bitmap.get_item(1)?.extract()?;
    if w == 0 {
        return Ok(());
    }
    // Borrowed out of the cached tuple rather than extracted: a page of text
    // is tens of thousands of these, and copying each bitmap on its way to
    // the blitter would undo the cache.
    let cov = bitmap.get_item(0)?;
    let cov = cov.cast::<PyBytes>()?;
    let h: i64 = bitmap.get_item(2)?.extract()?;
    let left: i64 = bitmap.get_item(3)?.extract()?;
    let top: i64 = bitmap.get_item(4)?.extract()?;
    surface.borrow_mut().blit_cov(
        cov.as_bytes(),
        w,
        h,
        trunc_i64(pen) + left,
        trunc_i64(baseline) + top,
        c,
    );
    Ok(())
}

/// Advance width of a string in pixels.
#[pyfunction]
pub fn measure_text(
    font: &Bound<'_, Font>,
    size: f64,
    text: &Bound<'_, PyAny>,
) -> PyResult<f64> {
    let mut total: i64 = 0;
    {
        let mut f = font.borrow_mut();
        for_each_char(text, |code| {
            let gid = f.glyph_of(code);
            total += f.advance_of(gid as i64);
        })?;
    }
    // Summed in font units and scaled once, so the width of a string is
    // exactly the sum of its characters' widths -- which is what the layout
    // engine's per-character width cache assumes.
    Ok(total as f64 * font.borrow().scale_of(size))
}

/// Add one subsample row's coverage for the span [x0, x1).
fn add_span(acc: &mut [f64], x0: f64, x1: f64, unit: f64, width: usize) {
    if x1 <= 0.0 || x0 >= width as f64 {
        return;
    }
    let x0 = x0.max(0.0);
    let x1 = x1.min(width as f64);
    let i0 = x0 as usize;
    let i1 = x1 as usize;
    if i0 == i1 {
        if let Some(a) = acc.get_mut(i0) {
            *a += (x1 - x0) * unit;
        }
        return;
    }
    if let Some(a) = acc.get_mut(i0) {
        *a += (i0 as f64 + 1.0 - x0) * unit;
    }
    for a in acc.iter_mut().take(i1.min(width)).skip(i0 + 1) {
        *a += unit;
    }
    if i1 < width {
        if let Some(a) = acc.get_mut(i1) {
            *a += (x1 - i1 as f64) * unit;
        }
    }
}

/// Python's `int()` on a float: truncate towards zero, saturating.
fn trunc_i64(v: f64) -> i64 {
    if v.is_nan() {
        return 0;
    }
    v.trunc().clamp(i64::MIN as f64, i64::MAX as f64) as i64
}

/// CRC-32 as PNG defines it, which is the same polynomial zlib uses.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

/// Wrap raw scanlines in the four chunks a truecolour PNG needs.
///
/// The deflate step goes back to Python's zlib rather than a Rust crate, and
/// deliberately: two deflate implementations agree on what inflates back but
/// not on the bytes they emit, and the screenshots this writes are compared
/// byte for byte against the ones the Python renderer produced. The work is
/// in C either way.
fn png_bytes<'py>(
    py: Python<'py>,
    width: i64,
    height: i64,
    raw: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let zlib = py.import("zlib")?;
    let packed = zlib.call_method1("compress", (PyBytes::new(py, raw), 6))?;
    let packed: Vec<u8> = packed.extract()?;

    let mut out: Vec<u8> = Vec::with_capacity(packed.len() + 64);
    out.extend_from_slice(b"\x89PNG\r\n\x1a\n");
    let mut header = Vec::with_capacity(13);
    header.extend_from_slice(&(width as u32).to_be_bytes());
    header.extend_from_slice(&(height as u32).to_be_bytes());
    header.extend_from_slice(&[8, 2, 0, 0, 0]);
    for (tag, payload) in [
        (b"IHDR", header.as_slice()),
        (b"IDAT", packed.as_slice()),
        (b"IEND", [].as_slice()),
    ] {
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        let mut body = Vec::with_capacity(4 + payload.len());
        body.extend_from_slice(tag);
        body.extend_from_slice(payload);
        out.extend_from_slice(&body);
        out.extend_from_slice(&crc32(&body).to_be_bytes());
    }
    Ok(PyBytes::new(py, &out))
}
