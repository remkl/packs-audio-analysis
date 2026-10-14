use crate::{Failure, config::Config};
use axum::http::{HeaderMap, header};
use std::{borrow::Cow, collections::HashMap};
use subtle::ConstantTimeEq;

#[derive(Debug, PartialEq)]
pub(crate) struct Payload {
    pub(crate) source_url: String,
    pub(crate) target_url: Option<String>,
}

fn invalid_body() -> Failure {
    Failure::new(422, "validate", "Invalid request body").classified("RequestValidationError")
}

fn parsing_error() -> Failure {
    Failure::new(400, "request", "There was an error parsing the body").classified("HTTPException")
}

fn auth_error(detail: &str) -> Failure {
    Failure::new(401, "request", detail).classified("HTTPException")
}

pub(crate) fn validate_request(
    config: &Config,
    headers: &HeaderMap,
    body: &[u8],
) -> Result<Payload, Failure> {
    // FastAPI parses JSON before dependencies, but validates the model after them.
    let fields = if !body.is_empty() && is_json(headers) {
        let text = decode_json(body)?;
        JsonParser::new(&text).parse()?
    } else {
        None
    };
    authenticate(config, headers)?;
    let mut fields = fields.ok_or_else(invalid_body)?;
    let source = fields
        .remove("sourceUrl")
        .or_else(|| fields.remove("source_url"));
    let target = fields
        .remove("targetUrl")
        .or_else(|| fields.remove("target_url"));
    let source_url = normalize_url(source)?;
    let target_url = match target {
        None | Some(Field::Null) => None,
        value => Some(normalize_url(value)?),
    };
    config.validate(&source_url, "Source")?;
    if let Some(target) = &target_url {
        config.validate(target, "Target")?;
    }
    Ok(Payload {
        source_url,
        target_url,
    })
}

fn is_json(headers: &HeaderMap) -> bool {
    // email.message strips the complete media type, but not either side of '/'.
    // The pinned FastAPI version enables strict_content_type, including no header.
    let Some(value) = headers.get(header::CONTENT_TYPE) else {
        return false;
    };
    let raw = String::from_utf8_lossy(value.as_bytes());
    let media_type = raw
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    let Some((main, subtype)) = media_type.split_once('/') else {
        return false;
    };
    main == "application"
        && !subtype.contains('/')
        && (subtype == "json" || subtype.ends_with("+json"))
}

fn authenticate(config: &Config, headers: &HeaderMap) -> Result<(), Failure> {
    let absent = || auth_error("Invalid audio analysis credentials");
    let malformed = || auth_error("Not authenticated");
    let Some(header) = headers.get(header::AUTHORIZATION) else {
        return Err(absent());
    };
    let bytes = header.as_bytes();
    let split = bytes
        .iter()
        .position(|byte| *byte == b' ')
        .unwrap_or(bytes.len());
    if !bytes[..split].eq_ignore_ascii_case(b"basic") {
        return Err(absent());
    }
    let param = bytes.get(split + 1..).unwrap_or_default();
    let decoded = decode_basic(param).ok_or_else(malformed)?;
    if !decoded.is_ascii() {
        return Err(malformed());
    }
    let colon = decoded
        .iter()
        .position(|byte| *byte == b':')
        .ok_or_else(malformed)?;
    let username_matches = decoded[..colon].ct_eq(config.username.as_bytes());
    let password_matches = decoded[colon + 1..].ct_eq(config.password.as_bytes());
    if bool::from(username_matches & password_matches) {
        Ok(())
    } else {
        Err(absent())
    }
}

fn decode_basic(input: &[u8]) -> Option<Vec<u8>> {
    if !input.is_ascii() {
        return None;
    }
    let mut output = Vec::with_capacity(input.len() / 4 * 3);
    let (mut position, mut padding, mut carry) = (0_u8, 0_u8, 0_u8);
    for byte in input {
        // Python b64decode(validate=False) ignores noise, accepts noncanonical bits,
        // and ignores everything after sufficient padding in an incomplete quartet.
        if *byte == b'=' {
            if position >= 2 {
                padding += 1;
                if position + padding >= 4 {
                    return Some(output);
                }
            }
            continue;
        }
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => continue,
        };
        padding = 0;
        match position {
            0 => carry = value,
            1 => {
                output.push((carry << 2) | (value >> 4));
                carry = value & 15;
            }
            2 => {
                output.push((carry << 4) | (value >> 2));
                carry = value & 3;
            }
            _ => output.push((carry << 6) | value),
        }
        position = (position + 1) % 4;
    }
    (position == 0).then_some(output)
}

fn normalize_url(value: Option<Field>) -> Result<String, Failure> {
    let Some(Field::String(value)) = value else {
        return Err(invalid_body());
    };
    // Pydantic HttpUrl checks the original UTF-8 byte count, before URL escaping.
    if value.len() > 2083 {
        return Err(invalid_body());
    }
    let url = reqwest::Url::parse(&value).map_err(|_| invalid_body())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(invalid_body());
    }
    Ok(url.into())
}

fn decode_json(bytes: &[u8]) -> Result<Cow<'_, str>, Failure> {
    // json.loads(bytes) detects UTF-8/16/32 independently of the charset parameter.
    let (encoding, offset) = if bytes.starts_with(&[0, 0, 254, 255]) {
        ("32be", 4)
    } else if bytes.starts_with(&[255, 254, 0, 0]) {
        ("32le", 4)
    } else if bytes.starts_with(&[254, 255]) {
        ("16be", 2)
    } else if bytes.starts_with(&[255, 254]) {
        ("16le", 2)
    } else if bytes.starts_with(&[239, 187, 191]) {
        ("8", 3)
    } else if bytes.len() >= 4 && bytes[0] == 0 && bytes[1] == 0 {
        ("32be", 0)
    } else if bytes.len() >= 4 && bytes[1] == 0 && bytes[2] == 0 && bytes[3] == 0 {
        ("32le", 0)
    } else if bytes.len() >= 2 && bytes[0] == 0 {
        ("16be", 0)
    } else if bytes.len() >= 2 && bytes[1] == 0 {
        ("16le", 0)
    } else {
        ("8", 0)
    };
    let bytes = &bytes[offset..];
    if encoding == "8" {
        return std::str::from_utf8(bytes).map(Cow::Borrowed).or_else(|_| {
            let mut remaining = bytes;
            let mut text = String::new();
            while !remaining.is_empty() {
                match std::str::from_utf8(remaining) {
                    Ok(valid) => {
                        text.push_str(valid);
                        break;
                    }
                    Err(error) => {
                        let prefix = error.valid_up_to();
                        text.push_str(
                            std::str::from_utf8(&remaining[..prefix])
                                .map_err(|_| parsing_error())?,
                        );
                        remaining = &remaining[prefix..];
                        if remaining.len() < 3
                            || remaining[0] != 0xed
                            || !(0xa0..=0xbf).contains(&remaining[1])
                            || !(0x80..=0xbf).contains(&remaining[2])
                        {
                            return Err(parsing_error());
                        }
                        let surrogate = (u32::from(remaining[0] & 15) << 12)
                            | (u32::from(remaining[1] & 63) << 6)
                            | u32::from(remaining[2] & 63);
                        append_codepoint(&mut text, surrogate)?;
                        remaining = &remaining[3..];
                    }
                }
            }
            Ok(Cow::Owned(text))
        });
    }
    let mut text = String::new();
    if encoding.starts_with("32") {
        if !bytes.len().is_multiple_of(4) {
            return Err(parsing_error());
        }
        for chunk in bytes.chunks_exact(4) {
            let array = [chunk[0], chunk[1], chunk[2], chunk[3]];
            let point = if encoding == "32le" {
                u32::from_le_bytes(array)
            } else {
                u32::from_be_bytes(array)
            };
            append_codepoint(&mut text, point)?;
        }
    } else {
        if !bytes.len().is_multiple_of(2) {
            return Err(parsing_error());
        }
        let units = bytes.chunks_exact(2).map(|chunk| {
            if encoding == "16le" {
                u16::from_le_bytes([chunk[0], chunk[1]])
            } else {
                u16::from_be_bytes([chunk[0], chunk[1]])
            }
        });
        for decoded in char::decode_utf16(units) {
            match decoded {
                Ok(character) => text.push(character),
                Err(error) => append_codepoint(&mut text, u32::from(error.unpaired_surrogate()))?,
            }
        }
    }
    Ok(Cow::Owned(text))
}

fn append_codepoint(text: &mut String, point: u32) -> Result<(), Failure> {
    if (0xd800..=0xdfff).contains(&point) {
        // Python permits lone surrogates in JSON strings. They remain invalid URLs.
        use std::fmt::Write;
        write!(text, "\\u{point:04x}").map_err(|_| parsing_error())?;
    } else {
        text.push(char::from_u32(point).ok_or_else(parsing_error)?);
    }
    Ok(())
}

#[derive(Debug)]
enum Field {
    Null,
    String(String),
    Other,
}
type Fields = HashMap<String, Field>;

// Shared with Python. Count the root request object as one container.
const JSON_MAX_CONTAINERS: usize = 64;

fn check_json_depth(text: &str) -> Result<(), Failure> {
    // Run before syntax validation in both services. Ignore quoted delimiters
    // and escaped quotes, and use constant space even for malformed input.
    let (mut depth, mut quoted, mut escaped) = (0_usize, false, false);
    for byte in text.bytes() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
        } else {
            match byte {
                b'"' => quoted = true,
                b'[' | b'{' => {
                    depth += 1;
                    if depth > JSON_MAX_CONTAINERS {
                        return Err(parsing_error());
                    }
                }
                b']' | b'}' => depth = depth.saturating_sub(1),
                _ => {}
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum ContainerKind {
    Array,
    Object,
}
struct Container {
    kind: ContainerKind,
    first: bool,
    capture_fields: bool,
}

// Only the four declared fields are retained. Unlike serde_json::Value, Python's
// decoder accepts nonfinite numbers, large integers and lone string surrogates in
// ignored fields. Syntax still has to be valid before authentication is checked.
struct JsonParser<'a> {
    text: &'a str,
    at: usize,
    fields: Option<Fields>,
}
impl<'a> JsonParser<'a> {
    fn new(text: &'a str) -> Self {
        Self {
            text,
            at: 0,
            fields: None,
        }
    }
    fn parse(mut self) -> Result<Option<Fields>, Failure> {
        check_json_depth(self.text)?;
        // A heap stack bounds nested input without consuming the server thread's
        // native stack. Ignored containers are validated but never materialized.
        let mut containers = Vec::new();
        self.value(&mut containers, None)?;
        while let Some(mut container) = containers.pop() {
            self.whitespace();
            let closing = match container.kind {
                ContainerKind::Array => b']',
                ContainerKind::Object => b'}',
            };
            if self.take(closing) {
                continue;
            }
            if !container.first && !self.take(b',') {
                return Err(invalid_body());
            }
            self.whitespace();
            let capture = match container.kind {
                ContainerKind::Array => None,
                ContainerKind::Object => {
                    let key = self.string()?;
                    self.whitespace();
                    if !self.take(b':') {
                        return Err(invalid_body());
                    }
                    key.filter(|key| {
                        container.capture_fields
                            && matches!(
                                key.as_str(),
                                "sourceUrl" | "source_url" | "targetUrl" | "target_url"
                            )
                    })
                }
            };
            container.first = false;
            containers.push(container);
            self.value(&mut containers, capture)?;
        }
        self.whitespace();
        if self.at != self.text.len() {
            return Err(invalid_body());
        }
        Ok(self.fields)
    }
    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.at).copied()
    }
    fn whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }
    fn take(&mut self, byte: u8) -> bool {
        if self.peek() == Some(byte) {
            self.at += 1;
            true
        } else {
            false
        }
    }
    fn value(
        &mut self,
        containers: &mut Vec<Container>,
        capture: Option<String>,
    ) -> Result<(), Failure> {
        self.whitespace();
        let value = match self.peek() {
            Some(b'"') => self.string()?.map_or(Field::Other, Field::String),
            Some(opening @ (b'{' | b'[')) => {
                if containers.len() >= JSON_MAX_CONTAINERS {
                    return Err(parsing_error());
                }
                self.at += 1;
                let capture_fields = opening == b'{' && containers.is_empty();
                if capture_fields {
                    self.fields = Some(Fields::new());
                }
                containers.push(Container {
                    kind: if opening == b'{' {
                        ContainerKind::Object
                    } else {
                        ContainerKind::Array
                    },
                    first: true,
                    capture_fields,
                });
                Field::Other
            }
            Some(b'n') => {
                self.literal("null")?;
                Field::Null
            }
            Some(b't') => {
                self.literal("true")?;
                Field::Other
            }
            Some(b'f') => {
                self.literal("false")?;
                Field::Other
            }
            Some(b'N') => {
                self.literal("NaN")?;
                Field::Other
            }
            Some(b'I') => {
                self.literal("Infinity")?;
                Field::Other
            }
            Some(b'-') if self.text[self.at..].starts_with("-Infinity") => {
                self.literal("-Infinity")?;
                Field::Other
            }
            Some(b'-' | b'0'..=b'9') => {
                self.number()?;
                Field::Other
            }
            _ => return Err(invalid_body()),
        };
        if let Some(key) = capture
            && let Some(fields) = &mut self.fields
        {
            fields.insert(key, value);
        }
        Ok(())
    }
    fn literal(&mut self, value: &str) -> Result<(), Failure> {
        if !self.text[self.at..].starts_with(value) {
            return Err(invalid_body());
        }
        self.at += value.len();
        Ok(())
    }
    fn string(&mut self) -> Result<Option<String>, Failure> {
        let start = self.at;
        if !self.take(b'"') {
            return Err(invalid_body());
        }
        loop {
            match self.peek() {
                None | Some(0..=31) => return Err(invalid_body()),
                Some(b'"') => {
                    self.at += 1;
                    return Ok(serde_json::from_str(&self.text[start..self.at]).ok());
                }
                Some(b'\\') => {
                    self.at += 1;
                    match self.peek() {
                        Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => {
                            self.at += 1
                        }
                        Some(b'u') => {
                            self.at += 1;
                            for _ in 0..4 {
                                if !self.peek().is_some_and(|byte| byte.is_ascii_hexdigit()) {
                                    return Err(invalid_body());
                                }
                                self.at += 1;
                            }
                        }
                        _ => return Err(invalid_body()),
                    }
                }
                _ => self.at += 1,
            }
        }
    }
    fn number(&mut self) -> Result<(), Failure> {
        self.take(b'-');
        let start = self.at;
        if !self.take(b'0') {
            if !matches!(self.peek(), Some(b'1'..=b'9')) {
                return Err(invalid_body());
            }
            while self.peek().is_some_and(|byte| byte.is_ascii_digit()) {
                self.at += 1;
            }
        }
        let digits = self.at - start;
        let mut integer = true;
        if self.take(b'.') {
            integer = false;
            self.digits()?;
        }
        if self.take(b'e') || self.take(b'E') {
            integer = false;
            if !self.take(b'+') {
                self.take(b'-');
            }
            self.digits()?;
        }
        if integer && digits > 4300 {
            return Err(parsing_error());
        }
        Ok(())
    }
    fn digits(&mut self) -> Result<(), Failure> {
        let start = self.at;
        while self.peek().is_some_and(|byte| byte.is_ascii_digit()) {
            self.at += 1;
        }
        if self.at == start {
            Err(invalid_body())
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;
    use base64::{Engine, engine::general_purpose::STANDARD};
    use std::{path::PathBuf, time::Duration};

    fn config() -> Config {
        Config {
            username: "analysis".into(),
            password: "secret:colon".into(),
            hosts: vec![
                "example.com".into(),
                "xn--xample-9ua.com".into(),
                "::1".into(),
            ],
            allow_http: false,
            workers: 2,
            queue: 4,
            queue_wait: Duration::from_secs(20),
            deadline: Duration::from_secs(100),
            max_download_bytes: 157286400,
            download_timeout_seconds: 90.0,
            max_duration_seconds: 300.0,
            data_dir: PathBuf::from("/tmp/audio-analysis"),
            temp_dir: PathBuf::from("/tmp/audio-analysis/tmp"),
        }
    }
    fn headers(auth: bool, content_type: Option<&str>) -> HeaderMap {
        let mut result = HeaderMap::new();
        if let Some(content_type) = content_type {
            result.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_str(content_type).unwrap(),
            );
        }
        if auth {
            result.insert(
                header::AUTHORIZATION,
                HeaderValue::from_str(&format!(
                    "bAsIc {}",
                    STANDARD.encode("analysis:secret:colon")
                ))
                .unwrap(),
            );
        }
        result
    }
    fn check_failure(
        result: Result<Payload, Failure>,
        status: u16,
        stage: &str,
        detail: &str,
        class: &str,
    ) {
        let error = result.unwrap_err();
        assert_eq!(
            (
                error.status,
                error.stage,
                error.detail.as_str(),
                error.error_class
            ),
            (status, stage, detail, class)
        );
    }
    #[test]
    fn parsing_and_authentication_have_the_python_precedence() {
        for media in [
            "application/json",
            "APPLICATION/JSON; charset=ascii",
            "application/problem+json",
        ] {
            check_failure(
                validate_request(&config(), &headers(false, Some(media)), b"{"),
                422,
                "validate",
                "Invalid request body",
                "RequestValidationError",
            );
            check_failure(
                validate_request(&config(), &headers(false, Some(media)), b"{}"),
                401,
                "request",
                "Invalid audio analysis credentials",
                "HTTPException",
            );
            check_failure(
                validate_request(&config(), &headers(false, Some(media)), b"\xff"),
                400,
                "request",
                "There was an error parsing the body",
                "HTTPException",
            );
        }
        for media in [None, Some("text/plain"), Some("application /json")] {
            check_failure(
                validate_request(&config(), &headers(false, media), b"{"),
                401,
                "request",
                "Invalid audio analysis credentials",
                "HTTPException",
            );
            check_failure(
                validate_request(&config(), &headers(true, media), b"{}"),
                422,
                "validate",
                "Invalid request body",
                "RequestValidationError",
            );
        }
    }
    #[test]
    fn aliases_override_field_names_including_null_and_invalid_values() {
        let headers = headers(true, Some("application/json"));
        let payload = validate_request(&config(), &headers, br#"{"source_url":"https://evil.invalid/","sourceUrl":"https://example.com","target_url":"https://evil.invalid/","targetUrl":null}"#).unwrap();
        assert_eq!(
            payload,
            Payload {
                source_url: "https://example.com/".into(),
                target_url: None
            }
        );
        for value in ["null", "42", "true", "[]", "{}", "\"bad\""] {
            let body = format!(r#"{{"source_url":"https://example.com","sourceUrl":{value}}}"#);
            assert_eq!(
                validate_request(&config(), &headers, body.as_bytes())
                    .unwrap_err()
                    .status,
                422
            );
        }
    }
    #[test]
    fn basic_auth_matches_permissive_base64_and_ascii_rules() {
        for encoded in ["Og==", "O=g==", "==Og==", "Og==junk", "Og==:", "Oh=="] {
            assert_eq!(decode_basic(encoded.as_bytes()).unwrap(), b":");
        }
        for encoded in ["Og", "Og=", "Y==="] {
            assert!(decode_basic(encoded.as_bytes()).is_none());
        }
        for (authorization, detail) in [
            ("Basic !!!", "Not authenticated"),
            ("Basic bm9jb2xvbg==", "Not authenticated"),
            ("Basic w6k6cA==", "Not authenticated"),
            ("Basic\tYTpi", "Invalid audio analysis credentials"),
            ("Bearer x", "Invalid audio analysis credentials"),
        ] {
            let mut headers = headers(false, Some("application/json"));
            headers.insert(
                header::AUTHORIZATION,
                HeaderValue::from_str(authorization).unwrap(),
            );
            check_failure(
                validate_request(&config(), &headers, b"{}"),
                401,
                "request",
                detail,
                "HTTPException",
            );
        }
    }
    #[test]
    fn urls_follow_pydantic_normalization_before_the_allowlist() {
        for (input, expected) in [
            ("HTTPS://EXAMPLE.COM:443", "https://example.com/"),
            (
                " https://Example.com/a b?q=a b#c d ",
                "https://example.com/a%20b?q=a%20b#c%20d",
            ),
            (
                "https://éxample.com/ü?q=ü",
                "https://xn--xample-9ua.com/%C3%BC?q=%C3%BC",
            ),
            ("https:example.com", "https://example.com/"),
            ("https://[::1]/", "https://[::1]/"),
        ] {
            let body = serde_json::json!({"sourceUrl":input}).to_string();
            assert_eq!(
                validate_request(
                    &config(),
                    &headers(true, Some("application/json")),
                    body.as_bytes()
                )
                .unwrap()
                .source_url,
                expected
            );
        }
        assert!(
            normalize_url(Some(Field::String(format!(
                "https://example.com/{}",
                "ü".repeat(1035)
            ))))
            .is_err()
        );
        assert!(normalize_url(Some(Field::String("ftp://example.com/".into()))).is_err());
    }
    #[test]
    fn json_nesting_has_the_same_explicit_limit_as_python() {
        for (depth, accepted) in [
            (62, true),
            (63, true),
            (64, false),
            (1010, false),
            (9982, false),
            (9983, false),
        ] {
            let body = format!(
                r#"{{"ignored":{}0{},"sourceUrl":"https://example.com"}}"#,
                "[".repeat(depth),
                "]".repeat(depth)
            );
            let result = validate_request(
                &config(),
                &headers(true, Some("application/json")),
                body.as_bytes(),
            );
            if accepted {
                assert!(result.is_ok(), "depth {depth}: {result:?}");
            } else {
                check_failure(
                    result,
                    400,
                    "request",
                    "There was an error parsing the body",
                    "HTTPException",
                );
            }
        }
    }
    #[test]
    fn untrusted_nesting_does_not_consume_the_native_stack() {
        std::thread::Builder::new()
            .stack_size(64 * 1024)
            .spawn(|| {
                for depth in [9_982, 100_000] {
                    let body = format!(
                        r#"{{"ignored":{}0{},"sourceUrl":"https://example.com"}}"#,
                        "[".repeat(depth),
                        "]".repeat(depth)
                    );
                    let result = validate_request(
                        &config(),
                        &headers(true, Some("application/json")),
                        body.as_bytes(),
                    );
                    check_failure(
                        result,
                        400,
                        "request",
                        "There was an error parsing the body",
                        "HTTPException",
                    );
                }
            })
            .unwrap()
            .join()
            .unwrap();
    }
    #[test]
    fn depth_preflight_skips_strings_and_runs_before_syntax_and_auth() {
        for depth in [63, 64, 65] {
            let mut value = serde_json::to_string(&r#"[]{}\""#.repeat(128)).unwrap();
            for level in 1..depth {
                value = if level % 2 == 0 {
                    format!(r#"{{"child":{value}}}"#)
                } else {
                    format!("[{value}]")
                };
            }
            let body = format!(r#"{{"sourceUrl":"https://example.com","ignored":{value}}}"#);
            for bytes in [
                body.as_bytes().to_vec(),
                body.encode_utf16().flat_map(u16::to_le_bytes).collect(),
                body.chars()
                    .flat_map(|character| u32::from(character).to_be_bytes())
                    .collect(),
            ] {
                let result =
                    validate_request(&config(), &headers(true, Some("application/json")), &bytes);
                assert_eq!(result.is_ok(), depth <= 64, "depth {depth}: {result:?}");
            }
        }
        let body = format!(
            r#"{{"broken":?,"ignored":{}0{}}}"#,
            "[".repeat(64),
            "]".repeat(64)
        );
        for authenticated in [false, true] {
            check_failure(
                validate_request(
                    &config(),
                    &headers(authenticated, Some("application/json")),
                    body.as_bytes(),
                ),
                400,
                "request",
                "There was an error parsing the body",
                "HTTPException",
            );
        }
        let unterminated_string = format!(r#"{{"ignored":"{}"#, "[".repeat(128));
        check_failure(
            validate_request(
                &config(),
                &headers(true, Some("application/json")),
                unterminated_string.as_bytes(),
            ),
            422,
            "validate",
            "Invalid request body",
            "RequestValidationError",
        );
    }
    #[test]
    fn nested_containers_do_not_capture_or_overwrite_root_fields() {
        let body = br#"{"ignored":[{"sourceUrl":"https://evil.invalid","nested":[[],{}]}],"sourceUrl":{},"sourceUrl":"https://example.com","targetUrl":[{"targetUrl":null}]}"#;
        let fields = JsonParser::new(std::str::from_utf8(body).unwrap())
            .parse()
            .unwrap()
            .unwrap();
        assert_eq!(fields.len(), 2);
        assert!(
            matches!(fields.get("sourceUrl"), Some(Field::String(value)) if value == "https://example.com")
        );
        assert!(matches!(fields.get("targetUrl"), Some(Field::Other)));
    }
    #[test]
    fn python_json_extensions_remain_valid_in_ignored_fields() {
        let body = br#"{"sourceUrl":"https://example.com","ignored":[NaN,Infinity,-Infinity,1e9999,99999999999999999999999999,"\ud800"]}"#;
        assert!(
            validate_request(&config(), &headers(true, Some("application/json")), body).is_ok()
        );
        let body = r#"{"sourceUrl":"https://example.com"}"#;
        for bytes in [
            body.encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
            body.chars()
                .flat_map(|character| u32::from(character).to_be_bytes())
                .collect(),
        ] {
            assert!(
                validate_request(&config(), &headers(true, Some("application/json")), &bytes)
                    .is_ok()
            );
        }
        for invalid in [
            "{\"ignored\":01}",
            "{\"ignored\":1.}",
            "[true,]",
            "{\"x\":\"\\x00\"}",
            "{} true",
        ] {
            assert!(JsonParser::new(invalid).parse().is_err());
        }
    }
}
