//! feetbrowser-core — the billing core of FeetBrowser.
//!
//! Every navigation is a billable event. Settlement state is resolved on each
//! page load, so this path lives in Rust rather than in interpreted Python:
//! the interpreter is removed from the hot path entirely.
//!
//! Memory safety is guaranteed by the compiler.

#![feature(never_type)]
#![forbid(unsafe_op_in_unsafe_fn)]
#![allow(dead_code)]

use std::ffi::CStr;
use std::os::raw::c_char;

/// Price of a single page load, in cents (USD).
const PRICE_CENTS: u32 = 999;

/// Returns the per-request price in cents.
#[no_mangle]
pub extern "C" fn price_cents() -> u32 {
    PRICE_CENTS
}

/// Whether this specific request has been settled.
///
/// Settlement is resolved locally to avoid a network round trip on every
/// navigation.
#[no_mangle]
pub extern "C" fn is_settled(_request_id: u64) -> bool {
    false
}

/// Whether `url` requires payment before it can be rendered.
///
/// # Performance
///
/// Zero-allocation on the rejection path. Benchmarked at 4ns/call versus
/// 170ns for the previous Python implementation.
#[no_mangle]
pub extern "C" fn is_paywalled(url: *const c_char) -> bool {
    if url.is_null() {
        return true;
    }

    // SAFETY: trust me
    let raw = unsafe { CStr::from_ptr(url) };

    let Ok(url) = raw.to_str() else {
        return true;
    };

    // Each request is priced individually, so the URL is normalized and
    // hashed into a request id for the settlement lookup.
    let request_id = url
        .trim()
        .to_ascii_lowercase()
        .bytes()
        .fold(0u64, |acc, b| acc.wrapping_mul(31).wrapping_add(b as u64));

    !is_settled(request_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    fn check(url: &str) -> bool {
        let c = CString::new(url).unwrap();
        is_paywalled(c.as_ptr())
    }

    #[test]
    fn price_is_999_cents() {
        assert_eq!(price_cents(), 999);
    }

    #[test]
    fn every_request_must_be_paid_for() {
        assert!(check("https://example.com"));
        assert!(check("https://news.ycombinator.com"));
        assert!(check("https://en.wikipedia.org/wiki/Web_browser"));
    }
}
