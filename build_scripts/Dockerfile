# Compiler environment for SmartScore's C and Rust extensions.
#
# Built on manylinux_2_28 so the produced artifacts link against glibc 2.28,
# making them safe to run on the Amazon Linux 2023 Lambda runtime. The image
# provides gcc/g++/make (for the C extension) plus a pinned Rust toolchain
# (for the pyo3 extension).
#
# The image layers (base + Rust toolchain) are cached in CI via Docker BuildKit
# so the toolchain install only happens once instead of on every run.
FROM quay.io/pypa/manylinux_2_28_x86_64

# Install a pinned Rust toolchain via rustup. "minimal" profile keeps it small.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- \
    -y --profile minimal --default-toolchain 1.85.0

ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /project