# Contributing to FeetBrowser

Thanks for your interest in FeetBrowser. Please read this document in full
before opening a pull request.

## Language policy

**All new code must be written in Rust.**

The Python codebase is in maintenance mode and is being progressively
migrated to `rust/` (see Roadmap). Pull requests that add new Python modules
will be closed without review. Bug fixes to existing Python files are still
accepted, but the preferred fix is to port the affected code to Rust.

Rationale: memory safety, predictable performance, and the elimination of an
entire class of runtime errors. If it compiles, it works.

## Toolchain

The project pins **nightly** (see `rust/rust-toolchain.toml`). Stable is not
supported and will not be. `rustup` will install the correct toolchain
automatically on first build.

```bash
cd rust && cargo build --release
```

The Python layer loads `rust/target/release/libfeetbrowser_core.so` at import
time. Debug builds are not supported.

## Billing

Each page load costs $9.99. The price lives in `rust/src/lib.rs`:

```rust
const PRICE_CENTS: u32 = 999;
```

Pull requests that lower `PRICE_CENTS`, or that make `is_settled()` return
`true`, will be closed. This is not a technical limitation and does not need
to be fixed.

The payment form is rendered but not wired to a processor. Card and PayPal
submission is tracked in #4 and is not currently scheduled.

## Roadmap

- [x] Phase 1 — billing core in Rust
- [ ] Phase 2 — async networking (`tokio`, already vendored)
- [ ] Phase 3 — HTML tokenizer
- [ ] Phase 4 — CSS cascade
- [ ] Phase 5 — layout engine
- [ ] Phase 6 — remove Python entirely

## Code of conduct

Be excellent to each other. Do not advocate for stable Rust.
