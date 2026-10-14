/// Match Python's decimal rounding of a finite binary float without first
/// rounding the scaled product. Callers use one or four decimal places.
pub(crate) fn round_decimal(value: f64, places: u32) -> f64 {
    if value == 0.0 || !value.is_finite() {
        return value;
    }
    let bits = value.to_bits();
    let encoded_exponent = ((bits >> 52) & 0x7ff) as i32;
    let exponent = if encoded_exponent == 0 {
        -1022
    } else {
        encoded_exponent - 1023
    };
    let shift = 52 - exponent;
    if shift <= 0 {
        return value;
    }
    let factor = 10_u128.pow(places);
    let mantissa = (bits & ((1_u64 << 52) - 1))
        | if encoded_exponent == 0 {
            0
        } else {
            1_u64 << 52
        };
    let scaled = u128::from(mantissa) * factor;
    let magnitude = if shift >= 128 {
        0
    } else {
        let quotient = scaled >> shift;
        let remainder = scaled & ((1_u128 << shift) - 1);
        let halfway = 1_u128 << (shift - 1);
        quotient + u128::from(remainder > halfway || (remainder == halfway && quotient & 1 != 0))
    };
    (magnitude as f64 / factor as f64).copysign(value)
}

#[cfg(test)]
mod tests {
    use super::round_decimal;

    #[test]
    fn waveform_ratio_should_match_python_at_decimal_boundary() {
        assert_eq!(round_decimal(2469.0 / 20000.0, 4), 0.1235);
    }

    #[test]
    fn exact_decimal_tie_should_round_to_even() {
        assert_eq!(round_decimal(125.25, 1), 125.2);
    }

    #[test]
    fn tiny_negative_value_should_keep_negative_zero() {
        assert_eq!(
            round_decimal(-f64::MIN_POSITIVE, 4).to_bits(),
            (-0.0_f64).to_bits()
        );
    }
}
