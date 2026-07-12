//! Response body / header hashing for the BBOT `HTTP_RESPONSE` shape.
//!
//! All three digests are byte-exact with the Python tools BBOT already uses so
//! hashes correlate across the pipeline:
//! - `md5` / `sha256` — lowercase hex, same as `hashlib.md5/.sha256`.
//! - `mmh3` — `murmur3::murmur3_32(bytes, seed=0) as i32`, which reproduces
//!   Python `mmh3.hash(bytes)` (MurmurHash3 x86_32, signed 32-bit) exactly,
//!   including negative values.

use std::fmt::Write as _;
use std::io::Cursor;

use md5::{Digest, Md5};
use sha2::Sha256;

/// The three digests over one byte string.
pub struct HashTriple {
    pub md5: String,
    pub mmh3: i32,
    pub sha256: String,
}

/// Canonical `Name: Value\r\nName: Value` header block — CRLF between entries,
/// NO status line, NO trailing CRLF, NO pseudo-headers. Byte-for-byte the same
/// construction as blacklanternsecurity/blasthttp's `build_raw_headers`, so
/// onyxweb's `raw_headers` (and its hash) match BBOT's response format.
/// Protocol-agnostic: h2/h3 pseudo-headers (`:status`) are never in the list.
pub fn build_raw_headers(headers: &[(String, String)]) -> String {
    let mut out = String::new();
    for (i, (k, v)) in headers.iter().enumerate() {
        if i > 0 {
            out.push_str("\r\n");
        }
        out.push_str(k);
        out.push_str(": ");
        out.push_str(v);
    }
    out
}

fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{b:02x}");
    }
    s
}

/// Compute md5 (hex), mmh3 (signed 32-bit), and sha256 (hex) over `bytes`.
pub fn hash_bytes(bytes: &[u8]) -> HashTriple {
    let md5 = to_hex(Md5::digest(bytes).as_slice());
    let sha256 = to_hex(Sha256::digest(bytes).as_slice());
    // murmur3_32 reads from a Reader; a byte slice can't error, so the Result
    // is infallible here.
    let mmh3 = murmur3::murmur3_32(&mut Cursor::new(bytes), 0).unwrap_or(0) as i32;
    HashTriple { md5, mmh3, sha256 }
}
