# Copyright (C) 2026 Alexander Eichhorn
# SPDX-License-Identifier: GPL-3.0-only

# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: cdivision=True

"""AOT kernels for ordered screens, error diffusion and TIFF PackBits."""

import numpy as np
cimport numpy as cnp
from cython.parallel cimport prange

ctypedef cnp.uint8_t u8
ctypedef cnp.uint16_t u16
ctypedef cnp.uint32_t u32
ctypedef cnp.int64_t i64

cnp.import_array()


cdef inline Py_ssize_t _positive_mod(Py_ssize_t value, Py_ssize_t divisor) noexcept nogil:
    cdef Py_ssize_t result = value % divisor
    if result < 0:
        result += divisor
    return result


cdef inline u32 _coordinate_hash32(
    Py_ssize_t x,
    Py_ssize_t y,
    u32 seed,
) noexcept nogil:
    """Return the full deterministic coordinate hash used by screen kernels."""

    cdef u32 value = seed ^ (<u32>x * <u32>0x9E3779B1) ^ (<u32>y * <u32>0x85EBCA77)
    value ^= value >> 16
    value *= <u32>0x7FEB352D
    value ^= value >> 15
    value *= <u32>0x846CA68B
    value ^= value >> 16
    return value


cdef inline u32 _coordinate_noise_7bit(
    Py_ssize_t x,
    Py_ssize_t y,
    u32 seed,
) noexcept nogil:
    """A repeatable coordinate hash used as Zhou-Fang's white-noise source.

    The paper defines ``rand(x,y) mod 128`` but deliberately does not mandate
    a particular pseudo-random generator.  A coordinate hash avoids Python
    RNG state in the hot loop, remains invariant across threads/processes and
    returns the exact required integer range 0..127.
    """

    return _coordinate_hash32(x, y, seed) & <u32>127


cdef inline double _interpolated_binary_entropy(
    double value,
    const double[::1] table,
) noexcept nogil:
    """Evaluate H(p) from a dense table without logarithms in the hot loop."""

    cdef double position, fraction
    cdef Py_ssize_t index
    if value <= 0.0 or value >= 1.0:
        return 0.0
    position = value * 65536.0
    index = <Py_ssize_t>position
    fraction = position - index
    return table[index] + fraction * (table[index + 1] - table[index])


cdef inline double _gaussian_horizontal_pixel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[::1] weights,
    Py_ssize_t y,
    Py_ssize_t x,
    Py_ssize_t width,
) noexcept nogil:
    cdef Py_ssize_t distance, left_x, right_x
    cdef double total = weights[0] * qtr_lut[codes[y, x]] / 65535.0
    for distance in range(1, 5):
        left_x = x - distance
        right_x = x + distance
        if left_x < 0:
            left_x = 0
        if right_x >= width:
            right_x = width - 1
        total = total + weights[distance] * (
            qtr_lut[codes[y, left_x]] + qtr_lut[codes[y, right_x]]
        ) / 65535.0
    return total


cdef inline double _gaussian_vertical_pixel(
    const double[:, ::1] horizontal,
    const double[::1] weights,
    Py_ssize_t y,
    Py_ssize_t x,
    Py_ssize_t height,
) noexcept nogil:
    cdef Py_ssize_t distance, top_y, bottom_y
    cdef double total = weights[0] * horizontal[y, x]
    for distance in range(1, 5):
        top_y = y - distance
        bottom_y = y + distance
        if top_y < 0:
            top_y = 0
        if bottom_y >= height:
            bottom_y = height - 1
        total = total + weights[distance] * (
            horizontal[top_y, x] + horizontal[bottom_y, x]
        )
    return total


cdef inline void _accumulate_clipped_error(
    double* errors,
    Py_ssize_t index,
    double original,
    double contribution,
) noexcept nogil:
    """Update a future sample exactly as an in-place clipped ED buffer."""

    cdef double value = original + errors[index] + contribution
    if value < 0.0:
        value = 0.0
    elif value > 1.0:
        value = 1.0
    errors[index] = value - original


def gaussian_highpass_qtr_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[::1] weights,
):
    """Compute ``Gaussian_sigma=1(I) - I`` with a separable 9-tap filter.

    This image-dependent signal is prepared only for entropy-constrained
    threshold modulation.  Both convolution passes are independent and use
    OpenMP; the serial dependency starts later in the error-diffusion kernel.
    Edge coordinates are clamped (nearest extension).
    """

    cdef Py_ssize_t height = codes.shape[0]
    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t local_y, local_x
    if height <= 0 or width <= 0:
        raise ValueError("QTR code raster must not be empty")
    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if weights.shape[0] != 5:
        raise ValueError("symmetric sigma=1 Gaussian kernel must contain five weights")

    cdef cnp.ndarray[cnp.float64_t, ndim=2] horizontal_array = np.empty(
        (height, width), dtype=np.float64
    )
    cdef cnp.ndarray[cnp.float64_t, ndim=2] highpass_array = np.empty(
        (height, width), dtype=np.float64
    )
    cdef double[:, ::1] horizontal = horizontal_array
    cdef double[:, ::1] highpass = highpass_array

    for local_y in prange(height, nogil=True, schedule="static"):
        for local_x in range(width):
            horizontal[local_y, local_x] = _gaussian_horizontal_pixel(
                codes, qtr_lut, weights, local_y, local_x, width
            )

    for local_y in prange(height, nogil=True, schedule="static"):
        for local_x in range(width):
            highpass[local_y, local_x] = (
                _gaussian_vertical_pixel(
                    horizontal, weights, local_y, local_x, height
                )
                - qtr_lut[codes[local_y, local_x]] / 65535.0
            )
    return highpass_array


cdef struct OstromoukhovThresholdPolicy:
    u8 marker


cdef struct ZhouFangThresholdPolicy:
    u8 marker


cdef struct EntropyZhouFangThresholdPolicy:
    u8 marker


ctypedef fused ErrorDiffusionThresholdPolicy:
    OstromoukhovThresholdPolicy
    ZhouFangThresholdPolicy
    EntropyZhouFangThresholdPolicy


cdef void _error_diffusion_qtr_scan(
    ErrorDiffusionThresholdPolicy policy,
    const u8[:, ::1] codes,
    const double* normalized_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    u8[:, :, ::1] output,
    Py_ssize_t process_height,
    Py_ssize_t source_row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    Py_ssize_t output_y_offset,
    int bit_depth,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_offset,
    Py_ssize_t noise_y_offset,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    double entropy_strength,
    double* current_error,
    double* next_error,
) noexcept nogil:
    """Run one compile-time-specialized variable-coefficient ED scan.

    ``ErrorDiffusionThresholdPolicy`` is a Cython fused type.  Cython emits a
    separate C function for each policy and resolves every ``Policy is ...``
    condition while generating C.  Consequently, the selected raster loop
    contains no runtime algorithm discriminator at all: the public wrapper
    chooses one specialization once, before the first pixel is processed.

    Keeping one source body preserves identical quantization, clipping,
    serpentine geometry and bit packing across all three algorithms without
    maintaining three error-prone copies of the diffusion loop.
    """

    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t endpoint_count = endpoint_values.shape[0]
    cdef Py_ssize_t local_y, local_x, global_x, byte_index
    cdef Py_ssize_t future_x, diagonal_x, error_index, clear_index
    cdef Py_ssize_t upper
    cdef int direction, level, bit_index
    cdef double original, adjusted, lower_value, upper_value
    cdef double local_fraction, threshold, quantized, error
    cdef u16 dot_code
    cdef u8 bit_mask
    cdef double* swap_error

    for local_y in range(process_height):
        direction = 1
        if serpentine and ((source_row_offset + local_y) & 1):
            direction = -1
            local_x = width - 1
        else:
            local_x = 0

        while 0 <= local_x < width:
            error_index = local_x + 1
            original = normalized_lut[codes[local_y, local_x]]
            adjusted = original + current_error[error_index]
            if adjusted < 0.0:
                adjusted = 0.0
            elif adjusted > 1.0:
                adjusted = 1.0

            level = <int>(adjusted * 255.0)
            if level > 255:
                level = 255

            # Locate the adjacent physical drop endpoints for multilevel ink.
            upper = 1
            while upper + 1 < endpoint_count and adjusted > endpoint_values[upper]:
                upper += 1
            lower_value = endpoint_values[upper - 1]
            upper_value = endpoint_values[upper]
            local_fraction = (adjusted - lower_value) / (upper_value - lower_value)

            if ErrorDiffusionThresholdPolicy is OstromoukhovThresholdPolicy:
                # Ostromoukhov varies diffusion coefficients but uses the
                # unmodulated midpoint quantizer from the 2001 paper.
                threshold = 0.5
            elif ErrorDiffusionThresholdPolicy is ZhouFangThresholdPolicy:
                # Zhou-Fang Eq. (1), on its original 8-bit scale.  This whole
                # expression exists only in the classic Zhou-Fang specialization.
                threshold = (
                    128.0
                    + _coordinate_noise_7bit(
                        local_x + noise_x_offset,
                        local_y + noise_y_offset,
                        seed,
                    ) * modulation[level]
                ) / 255.0
                if threshold < 0.0:
                    threshold = 0.0
                elif threshold > 1.0:
                    threshold = 1.0
            else:
                # Entropy-constrained Zhou-Fang has its own generated C loop.
                # Binary entropy suppresses the structure term near paper white
                # and solid black while retaining it in information-rich tones.
                threshold = (
                    128.0
                    + _coordinate_noise_7bit(
                        local_x + noise_x_offset,
                        local_y + noise_y_offset,
                        seed,
                    ) * modulation[level]
                ) / 255.0
                threshold += (
                    entropy_strength
                    * _interpolated_binary_entropy(adjusted, entropy_table)
                    * highpass[local_y, local_x]
                )
                if threshold < 0.0:
                    threshold = 0.0
                elif threshold > 1.0:
                    threshold = 1.0

            if local_fraction >= threshold:
                quantized = upper_value
                dot_code = endpoint_codes[upper]
            else:
                quantized = lower_value
                dot_code = endpoint_codes[upper - 1]
            error = adjusted - quantized

            # Two padded scanline buffers preserve the exact serial dependency
            # with O(width) memory.  Serpentine rows mirror only the geometry.
            future_x = local_x + direction
            if 0 <= future_x < width:
                _accumulate_clipped_error(
                    current_error,
                    future_x + 1,
                    normalized_lut[codes[local_y, future_x]],
                    error * coefficients[level, 0],
                )
            if source_row_offset + local_y + 1 < total_height:
                diagonal_x = local_x - direction
                if 0 <= diagonal_x < width:
                    _accumulate_clipped_error(
                        next_error,
                        diagonal_x + 1,
                        normalized_lut[codes[local_y + 1, diagonal_x]],
                        error * coefficients[level, 1],
                    )
                _accumulate_clipped_error(
                    next_error,
                    error_index,
                    normalized_lut[codes[local_y + 1, local_x]],
                    error * coefficients[level, 2],
                )

            global_x = x + local_x
            byte_index = global_x >> 3
            bit_mask = <u8>(0x80 >> (global_x & 7))
            for bit_index in range(bit_depth):
                if dot_code & (1 << bit_index):
                    output[bit_index, output_y_offset + local_y, byte_index] |= bit_mask
            local_x += direction

        swap_error = current_error
        current_error = next_error
        next_error = swap_error
        for clear_index in range(width + 2):
            next_error[clear_index] = 0.0


def error_diffusion_qtr_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    Py_ssize_t canvas_width,
    Py_ssize_t canvas_height,
    Py_ssize_t x,
    Py_ssize_t y,
    int bit_depth,
    int algorithm,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_offset,
    Py_ssize_t noise_y_offset,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    double entropy_strength,
):
    """Fused variable-coefficient ED, QTR lookup and multi-bit packing.

    ``algorithm`` is 0 for Ostromoukhov, 1 for classic Zhou-Fang and 2 for
    entropy-constrained Zhou-Fang.  The error dependency makes the scan
    intrinsically serial, so this loop intentionally does not use ``prange``;
    it does, however, release the GIL for the complete raster pass.

    Coefficient columns are the three unprocessed neighbours from the papers:
    A10 (next scan-direction pixel), A-11 (opposite diagonal on next row), and
    A01 (same column on next row).  On odd serpentine rows the geometry is
    mirrored, while coefficient meanings remain unchanged.
    """

    cdef Py_ssize_t height = codes.shape[0]
    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t endpoint_count = endpoint_values.shape[0]
    cdef Py_ssize_t row_bytes
    cdef Py_ssize_t lut_index
    cdef double normalized_lut[256]
    cdef double* current_error
    cdef double* next_error
    cdef OstromoukhovThresholdPolicy ostromoukhov_policy
    cdef ZhouFangThresholdPolicy zhou_fang_policy
    cdef EntropyZhouFangThresholdPolicy entropy_zhou_fang_policy

    if height <= 0 or width <= 0:
        raise ValueError("QTR code raster must not be empty")
    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if coefficients.shape[0] != 256 or coefficients.shape[1] != 3:
        raise ValueError("diffusion coefficients must have shape (256, 3)")
    if modulation.shape[0] != 256:
        raise ValueError("threshold modulation must have 256 entries")
    if endpoint_count < 2 or endpoint_codes.shape[0] != endpoint_count:
        raise ValueError("at least two matching drop endpoints are required")
    if canvas_width <= 0 or canvas_width % 8 or canvas_height <= 0:
        raise ValueError("invalid output canvas dimensions")
    if x < 0 or y < 0 or x + width > canvas_width or y + height > canvas_height:
        raise ValueError("error-diffusion raster exceeds the output canvas")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("bit_depth must be in range 1..8")
    if algorithm < 0 or algorithm > 2:
        raise ValueError("unknown error-diffusion algorithm")
    if algorithm == 2 and (highpass.shape[0] != height or highpass.shape[1] != width):
        raise ValueError("entropy high-pass image does not match the QTR raster")
    if entropy_table.shape[0] != 65537:
        raise ValueError("binary entropy table must have 65537 entries")

    for lut_index in range(256):
        normalized_lut[lut_index] = qtr_lut[lut_index] / 65535.0

    row_bytes = canvas_width // 8
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, canvas_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed
    cdef cnp.ndarray[cnp.float64_t, ndim=1] error_a_array = np.zeros(
        width + 2, dtype=np.float64
    )
    cdef cnp.ndarray[cnp.float64_t, ndim=1] error_b_array = np.zeros(
        width + 2, dtype=np.float64
    )
    cdef double[::1] error_a = error_a_array
    cdef double[::1] error_b = error_b_array
    current_error = &error_a[0]
    next_error = &error_b[0]

    # Dispatch exactly once.  Each call below targets a different fused-type
    # specialization, so no runtime algorithm check exists in the pixel loop.
    with nogil:
        if algorithm == 0:
            _error_diffusion_qtr_scan(
                ostromoukhov_policy, codes, normalized_lut, coefficients, modulation,
                endpoint_values, endpoint_codes, output, height, 0, height,
                canvas_width, x, y, bit_depth, serpentine, seed,
                noise_x_offset, noise_y_offset, highpass, entropy_table,
                entropy_strength, current_error, next_error,
            )
        elif algorithm == 1:
            _error_diffusion_qtr_scan(
                zhou_fang_policy, codes, normalized_lut, coefficients, modulation,
                endpoint_values, endpoint_codes, output, height, 0, height,
                canvas_width, x, y, bit_depth, serpentine, seed,
                noise_x_offset, noise_y_offset, highpass, entropy_table,
                entropy_strength, current_error, next_error,
            )
        else:
            _error_diffusion_qtr_scan(
                entropy_zhou_fang_policy, codes, normalized_lut, coefficients, modulation,
                endpoint_values, endpoint_codes, output, height, 0, height,
                canvas_width, x, y, bit_depth, serpentine, seed,
                noise_x_offset, noise_y_offset, highpass, entropy_table,
                entropy_strength, current_error, next_error,
            )
    return packed


cdef object _error_diffusion_qtr_band_kernel(
    ErrorDiffusionThresholdPolicy policy,
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    const double[::1] initial_error,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    Py_ssize_t output_height,
    Py_ssize_t row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    int bit_depth,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_base,
    Py_ssize_t noise_y_base,
    double entropy_strength,
):
    """Process one ED band while preserving the exact cross-band error state.

    ``codes`` contains the output rows plus one look-ahead row unless this is
    the final band.  That row lets the last processed row apply the same
    per-update clipping as the monolithic algorithm.  Only O(width) error and
    O(band_height*width) image memory is needed, which is essential for an A4
    page at 2880x2880 dpi (about 763 million samples per logical ink).
    """

    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t endpoint_count = endpoint_values.shape[0]
    cdef Py_ssize_t row_bytes = canvas_width // 8
    cdef Py_ssize_t lut_index
    cdef double normalized_lut[256]
    cdef double* current_error
    cdef double* next_error

    if output_height <= 0 or width <= 0 or output_height > codes.shape[0]:
        raise ValueError("invalid error-diffusion band dimensions")
    if row_offset < 0 or total_height <= 0 or row_offset + output_height > total_height:
        raise ValueError("error-diffusion band exceeds the source raster")
    if row_offset + output_height < total_height and codes.shape[0] < output_height + 1:
        raise ValueError("non-final error-diffusion band requires one look-ahead row")
    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if coefficients.shape[0] != 256 or coefficients.shape[1] != 3:
        raise ValueError("diffusion coefficients must have shape (256, 3)")
    if modulation.shape[0] != 256 or entropy_table.shape[0] != 65537:
        raise ValueError("invalid threshold-modulation lookup tables")
    if endpoint_count < 2 or endpoint_codes.shape[0] != endpoint_count:
        raise ValueError("at least two matching drop endpoints are required")
    if initial_error.shape[0] != width + 2:
        raise ValueError("cross-band error row has an invalid width")
    if canvas_width <= 0 or canvas_width % 8 or x < 0 or x + width > canvas_width:
        raise ValueError("error-diffusion band exceeds the output canvas")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("invalid error-diffusion configuration")
    if ErrorDiffusionThresholdPolicy is EntropyZhouFangThresholdPolicy and (
        highpass.shape[0] != output_height or highpass.shape[1] != width
    ):
        raise ValueError("entropy high-pass band does not match the output band")

    for lut_index in range(256):
        normalized_lut[lut_index] = qtr_lut[lut_index] / 65535.0

    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, output_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed
    cdef cnp.ndarray[cnp.float64_t, ndim=1] error_a_array = np.asarray(
        initial_error, dtype=np.float64
    ).copy()
    cdef cnp.ndarray[cnp.float64_t, ndim=1] error_b_array = np.zeros(
        width + 2, dtype=np.float64
    )
    cdef double[::1] error_a = error_a_array
    cdef double[::1] error_b = error_b_array
    current_error = &error_a[0]
    next_error = &error_b[0]

    # The caller has already selected this fused specialization before entering
    # the job's band loop.  This function therefore invokes one fixed scan.
    with nogil:
        _error_diffusion_qtr_scan(
            policy, codes, normalized_lut, coefficients, modulation,
            endpoint_values, endpoint_codes, output, output_height,
            row_offset, total_height, canvas_width, x, 0, bit_depth,
            serpentine, seed, noise_x_base, noise_y_base, highpass,
            entropy_table, entropy_strength, current_error, next_error,
        )

    # After every row the two pointers swap.  Return the buffer representing
    # errors already accumulated for the first row of the following band.
    if output_height % 2 == 0:
        return packed, error_a_array
    return packed, error_b_array


def ostromoukhov_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    const double[::1] initial_error,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    Py_ssize_t output_height,
    Py_ssize_t row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    int bit_depth,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_base,
    Py_ssize_t noise_y_base,
    double entropy_strength,
):
    """Process a band only with the Ostromoukhov specialization."""

    cdef OstromoukhovThresholdPolicy policy
    return _error_diffusion_qtr_band_kernel(
        policy, codes, qtr_lut, coefficients, modulation, endpoint_values,
        endpoint_codes, initial_error, highpass, entropy_table, output_height,
        row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
        noise_x_base, noise_y_base, entropy_strength,
    )


def zhou_fang_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    const double[::1] initial_error,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    Py_ssize_t output_height,
    Py_ssize_t row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    int bit_depth,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_base,
    Py_ssize_t noise_y_base,
    double entropy_strength,
):
    """Process a band only with the classic Zhou-Fang specialization."""

    cdef ZhouFangThresholdPolicy policy
    return _error_diffusion_qtr_band_kernel(
        policy, codes, qtr_lut, coefficients, modulation, endpoint_values,
        endpoint_codes, initial_error, highpass, entropy_table, output_height,
        row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
        noise_x_base, noise_y_base, entropy_strength,
    )


def entropy_zhou_fang_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    const double[::1] initial_error,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    Py_ssize_t output_height,
    Py_ssize_t row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    int bit_depth,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_base,
    Py_ssize_t noise_y_base,
    double entropy_strength,
):
    """Process a band only with entropy-constrained Zhou-Fang."""

    cdef EntropyZhouFangThresholdPolicy policy
    return _error_diffusion_qtr_band_kernel(
        policy, codes, qtr_lut, coefficients, modulation, endpoint_values,
        endpoint_codes, initial_error, highpass, entropy_table, output_height,
        row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
        noise_x_base, noise_y_base, entropy_strength,
    )


def error_diffusion_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const double[:, ::1] coefficients,
    const double[::1] modulation,
    const double[::1] endpoint_values,
    const u16[::1] endpoint_codes,
    const double[::1] initial_error,
    const double[:, ::1] highpass,
    const double[::1] entropy_table,
    Py_ssize_t output_height,
    Py_ssize_t row_offset,
    Py_ssize_t total_height,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    int bit_depth,
    int algorithm,
    bint serpentine,
    u32 seed,
    Py_ssize_t noise_x_base,
    Py_ssize_t noise_y_base,
    double entropy_strength,
):
    """Compatibility dispatcher; production binds a fixed kernel per job."""

    if algorithm == 0:
        return ostromoukhov_qtr_band_kernel(
            codes, qtr_lut, coefficients, modulation, endpoint_values,
            endpoint_codes, initial_error, highpass, entropy_table, output_height,
            row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
            noise_x_base, noise_y_base, entropy_strength,
        )
    if algorithm == 1:
        return zhou_fang_qtr_band_kernel(
            codes, qtr_lut, coefficients, modulation, endpoint_values,
            endpoint_codes, initial_error, highpass, entropy_table, output_height,
            row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
            noise_x_base, noise_y_base, entropy_strength,
        )
    if algorithm == 2:
        return entropy_zhou_fang_qtr_band_kernel(
            codes, qtr_lut, coefficients, modulation, endpoint_values,
            endpoint_codes, initial_error, highpass, entropy_table, output_height,
            row_offset, total_height, canvas_width, x, bit_depth, serpentine, seed,
            noise_x_base, noise_y_base, entropy_strength,
        )
    raise ValueError("unknown error-diffusion algorithm")


def ordered_band_kernel(
    const u16[:, ::1] values,
    const u16[:, ::1] matrix,
    const u16[::1] lower_bits,
    const u16[::1] upper_bits,
    const u32[::1] range_points,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    Py_ssize_t global_start,
    Py_ssize_t x_offset,
    Py_ssize_t y_offset,
    int bit_depth,
):
    """Dither and pack one row band with exact Gutenprint-compatible arithmetic."""

    cdef Py_ssize_t band_height = values.shape[0]
    cdef Py_ssize_t width = values.shape[1]
    cdef Py_ssize_t matrix_height = matrix.shape[0]
    cdef Py_ssize_t matrix_width = matrix.shape[1]
    cdef Py_ssize_t row_bytes
    cdef Py_ssize_t local_y, local_x, global_y, global_x
    cdef Py_ssize_t matrix_y, matrix_x, byte_index
    cdef int bit_index
    cdef u16 value, threshold, code
    cdef u8 bit_mask

    if canvas_width <= 0 or canvas_width % 8:
        raise ValueError("canvas_width must be a positive multiple of eight")
    if x < 0 or x + width > canvas_width:
        raise ValueError("Ordered band exceeds the canvas")
    if matrix_height <= 0 or matrix_width <= 0:
        raise ValueError("Ordered matrix must not be empty")
    if lower_bits.shape[0] != 65536 or upper_bits.shape[0] != 65536:
        raise ValueError("Ordered bit lookup tables must have 65536 entries")
    if range_points.shape[0] != 65536:
        raise ValueError("Ordered range lookup table must have 65536 entries")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("bit_depth must be in range 1..8")

    row_bytes = canvas_width // 8
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, band_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    for local_y in prange(
        band_height,
        nogil=True,
        schedule="static",
        use_threads_if=band_height >= 32,
    ):
        global_y = global_start + local_y
        matrix_y = _positive_mod(global_y + y_offset, matrix_height)
        for local_x in range(width):
            global_x = x + local_x
            matrix_x = _positive_mod(global_x + x_offset, matrix_width)
            value = values[local_y, local_x]
            threshold = matrix[matrix_y, matrix_x]
            code = lower_bits[value]
            if range_points[value] >= threshold:
                code = upper_bits[value]
            byte_index = global_x // 8
            bit_mask = <u8>(0x80 >> (global_x % 8))
            for bit_index in range(bit_depth):
                if code & (1 << bit_index):
                    output[bit_index, local_y, byte_index] |= bit_mask
    return packed


cdef void _ordered_qtr_1bit_row(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const u16[:, ::1] matrix,
    const u16[::1] lower_bits,
    const u16[::1] upper_bits,
    const u32[::1] range_points,
    u8[:, :, ::1] output,
    Py_ssize_t local_y,
    Py_ssize_t width,
    Py_ssize_t matrix_height,
    Py_ssize_t matrix_width,
    Py_ssize_t x,
    Py_ssize_t global_start,
    Py_ssize_t x_offset,
    Py_ssize_t y_offset,
) noexcept nogil:
    cdef Py_ssize_t local_x, global_y, global_x
    cdef Py_ssize_t matrix_y, matrix_x, byte_index
    cdef u16 value, threshold, dot_code
    cdef u8 bit_mask, output_byte

    global_y = global_start + local_y
    matrix_y = _positive_mod(global_y + y_offset, matrix_height)
    matrix_x = _positive_mod(x + x_offset, matrix_width)
    global_x = x
    byte_index = global_x >> 3
    bit_mask = <u8>(0x80 >> (global_x & 7))
    output_byte = output[0, local_y, byte_index]
    for local_x in range(width):
        value = qtr_lut[codes[local_y, local_x]]
        threshold = matrix[matrix_y, matrix_x]
        dot_code = lower_bits[value]
        if range_points[value] >= threshold:
            dot_code = upper_bits[value]
        if dot_code & 1:
            output_byte |= bit_mask

        matrix_x += 1
        if matrix_x == matrix_width:
            matrix_x = 0
        if bit_mask == 1:
            output[0, local_y, byte_index] = output_byte
            byte_index += 1
            bit_mask = <u8>0x80
            output_byte = 0
        else:
            bit_mask >>= 1
    if bit_mask != 0x80:
        output[0, local_y, byte_index] = output_byte


def ordered_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const u16[:, ::1] matrix,
    const u16[::1] lower_bits,
    const u16[::1] upper_bits,
    const u32[::1] range_points,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    Py_ssize_t global_start,
    Py_ssize_t x_offset,
    Py_ssize_t y_offset,
    int bit_depth,
):
    """Fuse exact 8-bit QTR LUT lookup, Ordered dither and bit packing."""

    cdef Py_ssize_t band_height = codes.shape[0]
    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t matrix_height = matrix.shape[0]
    cdef Py_ssize_t matrix_width = matrix.shape[1]
    cdef Py_ssize_t row_bytes
    cdef Py_ssize_t local_y, local_x, global_y, global_x
    cdef Py_ssize_t matrix_y, matrix_x, byte_index
    cdef int bit_index
    cdef u16 value, threshold, dot_code
    cdef u8 bit_mask

    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if canvas_width <= 0 or canvas_width % 8:
        raise ValueError("canvas_width must be a positive multiple of eight")
    if x < 0 or x + width > canvas_width:
        raise ValueError("Ordered QTR band exceeds the canvas")
    if matrix_height <= 0 or matrix_width <= 0:
        raise ValueError("Ordered matrix must not be empty")
    if lower_bits.shape[0] != 65536 or upper_bits.shape[0] != 65536:
        raise ValueError("Ordered bit lookup tables must have 65536 entries")
    if range_points.shape[0] != 65536:
        raise ValueError("Ordered range lookup table must have 65536 entries")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("bit_depth must be in range 1..8")

    row_bytes = canvas_width // 8
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, band_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    # The one-bit high-resolution modes dominate large-format workloads.
    # Build a complete output byte in a register instead of performing a
    # read/modify/write and two integer divisions for every device pixel.
    # Matrix X is a simple counter too; modulo belongs only at row start.
    if bit_depth == 1:
        for local_y in prange(
            band_height,
            nogil=True,
            schedule="static",
            use_threads_if=band_height >= 32,
        ):
            _ordered_qtr_1bit_row(
                codes,
                qtr_lut,
                matrix,
                lower_bits,
                upper_bits,
                range_points,
                output,
                local_y,
                width,
                matrix_height,
                matrix_width,
                x,
                global_start,
                x_offset,
                y_offset,
            )
    else:
        for local_y in prange(
            band_height,
            nogil=True,
            schedule="static",
            use_threads_if=band_height >= 32,
        ):
            global_y = global_start + local_y
            matrix_y = _positive_mod(global_y + y_offset, matrix_height)
            matrix_x = _positive_mod(x + x_offset, matrix_width)
            for local_x in range(width):
                global_x = x + local_x
                value = qtr_lut[codes[local_y, local_x]]
                threshold = matrix[matrix_y, matrix_x]
                dot_code = lower_bits[value]
                if range_points[value] >= threshold:
                    dot_code = upper_bits[value]
                byte_index = global_x >> 3
                bit_mask = <u8>(0x80 >> (global_x & 7))
                for bit_index in range(bit_depth):
                    if dot_code & (1 << bit_index):
                        output[bit_index, local_y, byte_index] |= bit_mask
                matrix_x += 1
                if matrix_x == matrix_width:
                    matrix_x = 0
    return packed


def blue_noise_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const u16[:, ::1] matrix,
    const u16[::1] lower_bits,
    const u16[::1] upper_bits,
    const u32[::1] range_points,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    Py_ssize_t global_start,
    Py_ssize_t x_offset,
    Py_ssize_t y_offset,
    Py_ssize_t channel_x_phase,
    Py_ssize_t channel_y_phase,
    u32 channel_seed,
    int bit_depth,
):
    """Screen one QTR band with deterministic aperiodic VAC tile placement.

    A small threshold tile repeated at exactly the same phase creates a strong
    wallpaper autocorrelation peak and can become a visible square mosaic in
    uniform step-wedge patches.  Every macro tile here still contains the
    complete progressive VAC rank permutation (therefore exactly the same mean
    density), but its toroidal phase and X/Y reflection are selected from a
    stable hash of the unbounded tile coordinates and logical ink identity.

    Translation and reflection preserve the base tile's blue-noise spectrum.
    Only neighbour relationships crossing a tile boundary differ; this removes
    exact short-period repetition without injecting density steps at the tile
    edges.  Channel phases follow Gutenprint's channel separation, while the
    channel seed prevents six monochrome inks from reinforcing one texture.
    """

    cdef Py_ssize_t band_height = codes.shape[0]
    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t matrix_height = matrix.shape[0]
    cdef Py_ssize_t matrix_width = matrix.shape[1]
    cdef Py_ssize_t row_bytes
    cdef Py_ssize_t local_y, local_x, global_y, global_x
    cdef Py_ssize_t shifted_x, shifted_y, current_shifted_x, tile_x, tile_y
    cdef Py_ssize_t tile_local_x, tile_local_y
    cdef Py_ssize_t matrix_x, matrix_y, phase_x, phase_y, byte_index
    cdef int bit_index
    cdef u16 value, threshold, dot_code
    cdef u8 bit_mask
    cdef u32 transform_x, transform_y
    cdef bint flip_x, flip_y

    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if canvas_width <= 0 or canvas_width % 8:
        raise ValueError("canvas_width must be a positive multiple of eight")
    if x < 0 or x + width > canvas_width:
        raise ValueError("Blue-noise QTR band exceeds the canvas")
    if matrix_height <= 0 or matrix_width <= 0:
        raise ValueError("Blue-noise matrix must not be empty")
    if lower_bits.shape[0] != 65536 or upper_bits.shape[0] != 65536:
        raise ValueError("Blue-noise bit lookup tables must have 65536 entries")
    if range_points.shape[0] != 65536:
        raise ValueError("Blue-noise range lookup table must have 65536 entries")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("bit_depth must be in range 1..8")

    row_bytes = canvas_width // 8
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, band_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    for local_y in prange(
        band_height,
        nogil=True,
        schedule="static",
        use_threads_if=band_height >= 32,
    ):
        global_y = global_start + local_y
        shifted_y = global_y + y_offset + channel_y_phase
        tile_local_y = _positive_mod(shifted_y, matrix_height)
        tile_y = (shifted_y - tile_local_y) // matrix_height

        shifted_x = x + x_offset + channel_x_phase
        tile_local_x = _positive_mod(shifted_x, matrix_width)
        transform_x = 0
        transform_y = 0

        for local_x in range(width):
            # Recompute the transform only at the first sample and when the
            # scan crosses into another base tile, not for every device pixel.
            if local_x == 0 or tile_local_x == 0:
                current_shifted_x = shifted_x + local_x
                tile_x = (current_shifted_x - tile_local_x) // matrix_width
                transform_x = _coordinate_hash32(tile_x, tile_y, channel_seed)
                transform_y = _coordinate_hash32(
                    tile_x,
                    tile_y,
                    channel_seed ^ <u32>0xA511E9B3,
                )
                phase_x = transform_x % matrix_width
                phase_y = transform_y % matrix_height
                flip_x = (transform_x & <u32>0x80000000) != 0
                flip_y = (transform_y & <u32>0x80000000) != 0
                if flip_y:
                    matrix_y = matrix_height - 1 - tile_local_y + phase_y
                else:
                    matrix_y = tile_local_y + phase_y
                if matrix_y >= matrix_height:
                    matrix_y = matrix_y - matrix_height

            if flip_x:
                matrix_x = matrix_width - 1 - tile_local_x + phase_x
            else:
                matrix_x = tile_local_x + phase_x
            if matrix_x >= matrix_width:
                matrix_x = matrix_x - matrix_width

            global_x = x + local_x
            value = qtr_lut[codes[local_y, local_x]]
            threshold = matrix[matrix_y, matrix_x]
            dot_code = lower_bits[value]
            if range_points[value] >= threshold:
                dot_code = upper_bits[value]
            byte_index = global_x // 8
            bit_mask = <u8>(0x80 >> (global_x % 8))
            for bit_index in range(bit_depth):
                if dot_code & (1 << bit_index):
                    output[bit_index, local_y, byte_index] |= bit_mask

            tile_local_x += 1
            if tile_local_x == matrix_width:
                tile_local_x = 0
    return packed


def very_fast_qtr_band_kernel(
    const u8[:, ::1] codes,
    const u16[::1] qtr_lut,
    const u16[:, ::1] matrix,
    Py_ssize_t canvas_width,
    Py_ssize_t x,
    Py_ssize_t global_start,
    Py_ssize_t x_offset,
    Py_ssize_t y_offset,
    int bit_depth,
    u16 largest_code,
):
    """Fuse QTR lookup and Gutenprint VeryFast largest-drop screening.

    Unlike the normal multi-level kernel, VeryFast does not interpolate
    between adjacent physical drop sizes.  Its 64x64 threshold decides only
    whether a pixel is empty or receives ``largest_code``.  The code is then
    split into ESC/P2 binary planes: code 3, for example, sets the same pixel
    bit in planes 0 and 1.  This removes drop interpolation and makes gradients
    visibly coarser.  On modern hardware the whole fused loop may be limited
    by memory and bit-plane packing, so the historical name does not guarantee
    a lower total runtime.

    ``global_start`` and offsets are kept separate so banding never restarts
    the matrix phase.  A page therefore produces identical dots whether it is
    processed as one allocation or as multiple bounded-memory bands.
    """

    cdef Py_ssize_t band_height = codes.shape[0]
    cdef Py_ssize_t width = codes.shape[1]
    cdef Py_ssize_t matrix_height = matrix.shape[0]
    cdef Py_ssize_t matrix_width = matrix.shape[1]
    cdef Py_ssize_t row_bytes = canvas_width // 8
    cdef Py_ssize_t local_y, local_x, global_y, global_x
    cdef Py_ssize_t matrix_y, matrix_x, byte_index
    cdef int bit_index
    cdef u16 value, threshold
    cdef u8 bit_mask

    if qtr_lut.shape[0] != 256:
        raise ValueError("QTR lookup table must have exactly 256 entries")
    if canvas_width <= 0 or canvas_width % 8 or x < 0 or x + width > canvas_width:
        raise ValueError("VeryFast QTR band exceeds the canvas")
    if matrix_height <= 0 or matrix_width <= 0:
        raise ValueError("VeryFast matrix must not be empty")
    if bit_depth < 1 or bit_depth > 8:
        raise ValueError("bit_depth must be in range 1..8")

    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (bit_depth, band_height, row_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    for local_y in prange(
        band_height,
        nogil=True,
        schedule="static",
        use_threads_if=band_height >= 32,
    ):
        global_y = global_start + local_y
        matrix_y = _positive_mod(global_y + y_offset, matrix_height)
        matrix_x = _positive_mod(x + x_offset, matrix_width)
        for local_x in range(width):
            global_x = x + local_x
            value = qtr_lut[codes[local_y, local_x]]
            threshold = matrix[matrix_y, matrix_x]
            # Gutenprint compares the 16-bit contone value directly with the
            # screen (>= is intentional).  Its outer ``if (raw[i])`` check is
            # retained too: zero never prints at the matrix's zero cell.
            if value != 0 and value >= threshold:
                byte_index = global_x // 8
                bit_mask = <u8>(0x80 >> (global_x % 8))
                for bit_index in range(bit_depth):
                    if largest_code & (1 << bit_index):
                        output[bit_index, local_y, byte_index] |= bit_mask
            matrix_x += 1
            if matrix_x == matrix_width:
                matrix_x = 0
    return packed


cdef Py_ssize_t _packbits_encode_into(
    const u8* data,
    Py_ssize_t size,
    u8* output,
) noexcept nogil:
    cdef Py_ssize_t index = 0
    cdef Py_ssize_t literal_start, literal_length, literal_index
    cdef Py_ssize_t chunk, run, output_index = 0
    while index < size:
        literal_start = index
        while index < size:
            run = 1
            while index + run < size and data[index + run] == data[index] and run < 128:
                run += 1
            if run >= 3:
                break
            index += run
            if index - literal_start >= 128:
                break

        literal_length = index - literal_start
        literal_index = literal_start
        while literal_length > 0:
            chunk = 128 if literal_length > 128 else literal_length
            output[output_index] = <u8>(chunk - 1)
            output_index += 1
            for run in range(chunk):
                output[output_index + run] = data[literal_index + run]
            output_index += chunk
            literal_index += chunk
            literal_length -= chunk

        if index >= size:
            break

        run = 1
        while index + run < size and data[index + run] == data[index] and run < 128:
            run += 1
        output[output_index] = <u8>((1 - run) & 0xFF)
        output[output_index + 1] = data[index]
        output_index += 2
        index += run
    return output_index


def packbits_encode(const u8[::1] data):
    """Encode one row exactly like the retained Python semantic reference."""

    cdef Py_ssize_t size = data.shape[0]
    cdef Py_ssize_t maximum, output_index
    if size == 0:
        return b""

    maximum = (size * 2) + 2
    cdef bytearray storage = bytearray(maximum)
    cdef u8[::1] output = storage
    with nogil:
        output_index = _packbits_encode_into(&data[0], size, &output[0])
    return bytes(storage[:output_index])


def packbits_encode_rows(const u8[:, ::1] rows):
    """Encode a dense row band with one Python/Cython boundary crossing."""

    cdef Py_ssize_t row_count = rows.shape[0]
    cdef Py_ssize_t row_bytes = rows.shape[1]
    cdef Py_ssize_t maximum, row
    cdef list result = []
    if row_count == 0:
        return ()
    if row_bytes == 0:
        for row in range(row_count):
            result.append(b"")
        return tuple(result)
    maximum = (row_bytes * 2) + 2
    cdef cnp.ndarray[u8, ndim=2] storage_array = np.empty(
        (row_count, maximum), dtype=np.uint8
    )
    cdef cnp.ndarray[i64, ndim=1] length_array = np.empty(
        row_count, dtype=np.int64
    )
    cdef u8[:, ::1] storage = storage_array
    cdef i64[::1] lengths = length_array
    for row in prange(
        row_count, nogil=True, schedule="static", use_threads_if=row_count >= 32
    ):
        lengths[row] = _packbits_encode_into(
            &rows[row, 0], row_bytes, &storage[row, 0]
        )
    for row in range(row_count):
        result.append(storage_array[row, :length_array[row]].tobytes())
    return tuple(result)


def packbits_decode_rows(
    const u8[::1] data,
    Py_ssize_t start,
    Py_ssize_t expected_length,
    Py_ssize_t row_count,
):
    """Decode consecutive PackBits rows while crossing Python only once.

    ESC/P2 does not store a compressed length for each row.  The decoder must
    therefore finish one declared-width row before it knows where the next row
    starts.  Keeping that small state machine in C is substantially faster for
    high-resolution jobs while the caller still receives ordinary immutable
    ``bytes`` rows for independent validation.
    """

    cdef Py_ssize_t size = data.shape[0]
    cdef Py_ssize_t row_number, row_start, index = start
    cdef Py_ssize_t output_index, count, copy_index
    cdef int control, error
    cdef bytearray storage
    cdef u8[::1] output
    cdef list decoded_rows = []
    cdef list encoded_lengths = []

    if start < 0 or start > size:
        raise ValueError("PackBits start offset outside input")
    if expected_length < 0:
        raise ValueError("PackBits expected length must not be negative")
    if row_count < 0:
        raise ValueError("PackBits row count must not be negative")

    for row_number in range(row_count):
        row_start = index
        output_index = 0
        storage = bytearray(expected_length)
        output = storage
        error = 0

        # Error code is inspected after reacquiring the GIL.  This keeps the
        # hot packet loop free of Python calls without weakening diagnostics.
        with nogil:
            while output_index < expected_length:
                if index >= size:
                    error = 1  # truncated stream/control byte
                    break
                control = data[index]
                index += 1
                if control <= 127:
                    count = control + 1
                    if index + count > size:
                        error = 2  # truncated literal packet
                        break
                    if output_index + count > expected_length:
                        error = 4  # packet crosses the declared row boundary
                        break
                    for copy_index in range(count):
                        output[output_index + copy_index] = data[index + copy_index]
                    output_index += count
                    index += count
                elif control >= 129:
                    if index >= size:
                        error = 3  # repeated-run value is missing
                        break
                    count = 257 - control
                    if output_index + count > expected_length:
                        error = 4
                        break
                    for copy_index in range(count):
                        output[output_index + copy_index] = data[index]
                    output_index += count
                    index += 1
                # 128 is the TIFF PackBits NOP and consumes no output.

        if error:
            if error == 1:
                reason = "truncated PackBits stream"
            elif error == 2:
                reason = "truncated PackBits literal"
            elif error == 3:
                reason = "truncated PackBits run"
            else:
                reason = "PackBits row exceeds declared length"
            raise ValueError(f"row {row_number}: {reason}")

        decoded_rows.append(bytes(storage))
        encoded_lengths.append(index - row_start)

    return tuple(decoded_rows), tuple(encoded_lengths), index - start


cdef void _weave_fold_row(
    const u8[:, ::1] low_bits,
    const u8[:, ::1] high_bits,
    u8[:, :, ::1] output,
    Py_ssize_t row,
    Py_ssize_t width,
    Py_ssize_t reduced_width,
    int horizontal_passes,
    int vertical_passes,
) noexcept nogil:
    cdef Py_ssize_t horizontal, reduced_x, source_x, source_byte
    cdef Py_ssize_t output_byte, active_index, output_index
    cdef int vertical, shift
    cdef u8 mask, code
    for horizontal in range(horizontal_passes):
        active_index = 0
        for reduced_x in range(reduced_width):
            source_x = horizontal + reduced_x * horizontal_passes
            if source_x >= width:
                continue
            source_byte = source_x // 8
            mask = <u8>(0x80 >> (source_x % 8))
            code = 0
            if low_bits[row, source_byte] & mask:
                code = 1
            if high_bits[row, source_byte] & mask:
                code |= 2
            if code == 0:
                continue
            vertical = <int>(active_index % vertical_passes)
            active_index += 1
            output_index = vertical * horizontal_passes + horizontal
            output_byte = reduced_x // 4
            shift = <int>(6 - 2 * (reduced_x % 4))
            output[row, output_index, output_byte] |= <u8>(code << shift)


cdef void _weave_split_1bit_row(
    const u8[:, ::1] bits,
    u8[:, :, ::1] output,
    Py_ssize_t row,
    Py_ssize_t width,
    Py_ssize_t reduced_width,
    int horizontal_passes,
    int vertical_passes,
) noexcept nogil:
    """Split an already packed one-bit row without uint16 unpack/repack."""

    cdef Py_ssize_t horizontal, reduced_x, source_x, source_byte
    cdef Py_ssize_t output_byte, active_index, output_index
    cdef int vertical
    cdef u8 source_mask, output_mask
    for horizontal in range(horizontal_passes):
        active_index = 0
        for reduced_x in range(reduced_width):
            source_x = horizontal + reduced_x * horizontal_passes
            if source_x >= width:
                continue
            source_byte = source_x >> 3
            source_mask = <u8>(0x80 >> (source_x & 7))
            if not (bits[row, source_byte] & source_mask):
                continue
            vertical = <int>(active_index % vertical_passes)
            active_index += 1
            output_index = vertical * horizontal_passes + horizontal
            output_byte = reduced_x >> 3
            output_mask = <u8>(0x80 >> (reduced_x & 7))
            output[row, output_index, output_byte] |= output_mask


def weave_split_1bit_kernel(
    const u8[:, ::1] bits,
    Py_ssize_t width,
    int horizontal_passes,
    int vertical_passes,
):
    """Fuse one-bit horizontal/vertical split while preserving packed rows."""

    cdef Py_ssize_t height = bits.shape[0]
    cdef Py_ssize_t row_bytes = bits.shape[1]
    cdef Py_ssize_t reduced_width
    cdef Py_ssize_t packed_bytes
    cdef Py_ssize_t row
    if width <= 0 or row_bytes != (width + 7) // 8:
        raise ValueError("model96 packed row width mismatch")
    if horizontal_passes <= 0 or vertical_passes <= 0:
        raise ValueError("model96 pass counts must be positive")

    reduced_width = (width + horizontal_passes - 1) // horizontal_passes
    packed_bytes = (reduced_width + 7) // 8
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (height, vertical_passes * horizontal_passes, packed_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    # The common 1x1 case is already in its final packed representation.
    if horizontal_passes == 1 and vertical_passes == 1:
        packed[:, 0, :] = bits
        return packed

    for row in prange(
        height, nogil=True, schedule="static", use_threads_if=height >= 32
    ):
        _weave_split_1bit_row(
            bits,
            output,
            row,
            width,
            reduced_width,
            horizontal_passes,
            vertical_passes,
        )
    return packed


def weave_unpack_split_fold_kernel(
    const u8[:, ::1] low_bits,
    const u8[:, ::1] high_bits,
    Py_ssize_t width,
    int horizontal_passes,
    int vertical_passes,
):
    """Fuse model96 2-bit unpack, h/v split and big-endian two-bit fold."""

    cdef Py_ssize_t height = low_bits.shape[0]
    cdef Py_ssize_t row_bytes = low_bits.shape[1]
    cdef Py_ssize_t reduced_width
    cdef Py_ssize_t packed_bytes
    cdef Py_ssize_t row
    if high_bits.shape[0] != height or high_bits.shape[1] != row_bytes:
        raise ValueError("model96 bit planes must have identical geometry")
    if width <= 0 or row_bytes != (width + 7) // 8:
        raise ValueError("model96 packed row width mismatch")
    if horizontal_passes <= 0 or vertical_passes <= 0:
        raise ValueError("model96 pass counts must be positive")

    reduced_width = (width + horizontal_passes - 1) // horizontal_passes
    packed_bytes = 2 * ((reduced_width + 7) // 8)
    cdef cnp.ndarray[u8, ndim=3] packed = np.zeros(
        (height, vertical_passes * horizontal_passes, packed_bytes), dtype=np.uint8
    )
    cdef u8[:, :, ::1] output = packed

    for row in prange(
        height, nogil=True, schedule="static", use_threads_if=height >= 32
    ):
        _weave_fold_row(
            low_bits,
            high_bits,
            output,
            row,
            width,
            reduced_width,
            horizontal_passes,
            vertical_passes,
        )
    return packed


def weave_assignment_kernel(
    Py_ssize_t first_source_row,
    Py_ssize_t height,
    Py_ssize_t head_offset,
    int subpass_count,
    Py_ssize_t separation,
    Py_ssize_t jets,
    Py_ssize_t oversampling,
    Py_ssize_t subblocks_per_passblock,
    Py_ssize_t passes_per_subblock,
    Py_ssize_t first_premapped,
    Py_ssize_t first_normal,
    Py_ssize_t first_postmapped,
    Py_ssize_t first_row,
    Py_ssize_t last_row,
    Py_ssize_t pre_base,
    const i64[::1] pre_map,
    const i64[::1] pre_stagger,
    Py_ssize_t post_base,
    const i64[::1] post_map,
    const i64[::1] post_stagger,
):
    """Bulk exact _CookedWeave.row_parameters for one physical path."""

    cdef Py_ssize_t advance_basis = jets // oversampling
    cdef Py_ssize_t source_index, subpass, row, adjusted_row
    cdef Py_ssize_t subblock_offset, subpass_block, band, base_row
    cdef Py_ssize_t pass_in_band, offset, pass_div_separation
    cdef Py_ssize_t pass_mod_separation, off_mod_separation, rounded_jets
    cdef Py_ssize_t raw_pass, jet, start, stagger, pass_index
    cdef Py_ssize_t missing, jets_used, extra, physical_start, physical_end
    cdef Py_ssize_t map_index
    if height <= 0 or subpass_count <= 0:
        raise ValueError("weave assignment geometry must be positive")
    if advance_basis < 1:
        advance_basis = 1
    cdef cnp.ndarray[i64, ndim=3] assignments = np.empty(
        (height, subpass_count, 6), dtype=np.int64
    )
    cdef i64[:, :, ::1] output = assignments

    with nogil:
      for source_index in range(height):
        row = first_source_row + source_index + head_offset
        for subpass in range(subpass_count):
            adjusted_row = row + separation * jets
            subblock_offset = adjusted_row % subblocks_per_passblock
            if subblock_offset % 2 == 0:
                subpass_block = subblock_offset // 2
            else:
                subpass_block = subblocks_per_passblock - (subblock_offset + 1) // 2
            band = adjusted_row // (separation * jets)
            base_row = adjusted_row - subblock_offset - band * separation * jets
            pass_in_band = base_row // advance_basis
            offset = base_row % advance_basis
            pass_div_separation = pass_in_band // separation
            pass_mod_separation = pass_in_band % separation
            off_mod_separation = offset % separation
            while (
                off_mod_separation != 0
                or pass_div_separation != subpass
                or pass_mod_separation // passes_per_subblock != subpass_block
            ):
                offset += advance_basis
                pass_in_band -= 1
                if pass_in_band >= 0:
                    pass_mod_separation -= 1
                    if pass_mod_separation < 0:
                        pass_mod_separation += separation
                        pass_div_separation -= 1
                    if advance_basis < separation:
                        off_mod_separation += advance_basis
                        if off_mod_separation >= separation:
                            off_mod_separation -= separation
                    elif advance_basis > separation:
                        off_mod_separation = offset % separation
                else:
                    rounded_jets = (advance_basis * oversampling) % jets
                    band -= 1
                    pass_in_band += separation * oversampling
                    offset += separation * (jets - rounded_jets)
                    pass_div_separation = pass_in_band // separation
                    pass_mod_separation = pass_in_band % separation
                    off_mod_separation = offset % separation
            raw_pass = band * oversampling * separation + pass_in_band
            jet = (offset // separation) % jets
            start = adjusted_row - jet * separation - separation * jets
            stagger = 0
            if raw_pass < first_normal:
                map_index = raw_pass - pre_base
                pass_index = pre_map[map_index]
                stagger = pre_stagger[map_index]
            elif raw_pass >= first_postmapped:
                map_index = raw_pass - post_base
                pass_index = post_map[map_index]
                stagger = post_stagger[map_index]
            else:
                pass_index = raw_pass - first_premapped
            start += stagger * separation
            jet -= stagger
            missing = 0
            jets_used = jets
            if stagger < 0:
                missing = -stagger
                jets_used += stagger
            extra = first_row - (start + separation * missing)
            if extra > 0:
                extra = (extra + separation - 1) // separation
                jets_used -= extra
                missing += extra
            extra = start + separation * (missing + jets_used - 1) - last_row
            if extra > 0:
                jets_used -= (extra + separation - 1) // separation
            physical_start = start + separation * missing
            physical_end = physical_start + separation * (jets_used - 1)
            output[source_index, subpass, 0] = pass_index
            output[source_index, subpass, 1] = jet
            output[source_index, subpass, 2] = start
            output[source_index, subpass, 3] = physical_start
            output[source_index, subpass, 4] = physical_end
            output[source_index, subpass, 5] = missing
    return assignments


def self_test():
    """Load the AOT module and verify both production entry points."""

    values = np.zeros((2, 8), dtype=np.uint16)
    matrix = np.zeros((1, 1), dtype=np.uint16)
    lower = np.zeros(65536, dtype=np.uint16)
    upper = np.zeros(65536, dtype=np.uint16)
    ranges = np.zeros(65536, dtype=np.uint32)
    packed = ordered_band_kernel(
        values, matrix, lower, upper, ranges, 8, 0, 0, 0, 0, 2
    )
    if packed.shape != (2, 2, 1) or np.any(packed):
        raise RuntimeError("Cython Ordered self-test failed")
    if packbits_encode(b"ABCCCDE") != b"\x01AB\xfeC\x01DE":
        raise RuntimeError("Cython PackBits self-test failed")
    decoded, lengths, consumed = packbits_decode_rows(
        b"\x01AB\xfeC\x01DE\xfaZ", 0, 7, 2
    )
    if decoded != (b"ABCCCDE", b"ZZZZZZZ") or lengths != (8, 2) or consumed != 10:
        raise RuntimeError("Cython PackBits decode self-test failed")
    weave_bits = np.zeros((2, 1), dtype=np.uint8)
    weave_bits[0, 0] = 0x80
    folded = weave_unpack_split_fold_kernel(weave_bits, weave_bits, 8, 1, 1)
    if folded.shape != (2, 1, 2) or folded[0, 0, 0] != 0xC0:
        raise RuntimeError("Cython weave raster self-test failed")
    empty_map = np.zeros(1, dtype=np.int64)
    assignments = weave_assignment_kernel(
        1, 2, 0, 1, 1, 1, 1, 1, 1, 0, 0, 100, 1, 2,
        0, empty_map, empty_map, 0, empty_map, empty_map,
    )
    if assignments.shape != (2, 1, 6):
        raise RuntimeError("Cython weave assignment self-test failed")
    return {"engine": "cython-kernels-v1", "aot": True}
