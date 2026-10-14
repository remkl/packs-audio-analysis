fn main() {
    cc::Build::new()
        .cpp(true)
        .file("native.cpp")
        .include("/usr/local/include/essentia")
        .include("/usr/include/eigen3")
        .flag("-std=c++11")
        .compile("pax_essentia_bridge");
    println!("cargo:rustc-link-search=native=/usr/local/lib");
    println!("cargo:rustc-link-lib=essentia");
    println!("cargo:rerun-if-changed=native.cpp");
}
