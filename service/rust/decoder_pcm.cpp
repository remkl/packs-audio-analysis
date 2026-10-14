// Migration proof: export the production bounded decoder's float32 samples.
#include "native.cpp"
#include <fstream>
#include <iostream>

int main(int argc, char** argv) {
    try {
        if (argc != 3) throw std::runtime_error("Pass input and PCM output paths");
        pax_init();
        std::vector<Real> audio;
        decode_bounded(argv[1], audio, decode_limit(300));
        if (audio.empty()) throw std::runtime_error("Expected nonempty decoded audio");
        std::ofstream output(argv[2], std::ios::binary);
        output.write(reinterpret_cast<const char*>(audio.data()), audio.size() * sizeof(Real));
        output.close();
        if (!output) throw std::runtime_error("Could not write PCM proof");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
    } catch (...) {
        std::cerr << "Bounded decoding failed" << std::endl;
    }
    return 1;
}
