#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#ifndef SINC_SOURCE
#define SINC_SOURCE "/samplerate/src/src_sinc.c"
#endif
#include SINC_SOURCE

int main(int argc, char **argv) {
  int channels = argc > 1 ? atoi(argv[1]) : 1;
  SINC_FILTER *filter = calloc(1, sizeof(*filter) + 256 * sizeof(float));
  float *coeffs = malloc(5 * sizeof(float));
  for (int i = 0; i < 5; i++) coeffs[i] = (4-i)*0.25f;
  filter->coeffs = coeffs;
  filter->coeff_half_len = 4;
  filter->channels = channels;
  filter->b_current = 12 * channels;
  for (int i = 0; i < 256; i++) filter->buffer[i] = 1.0f;
  float output[8] = {0};
  increment_t increment = int_to_fp(1), start = 0;
  if (channels == 1) output[0] = calc_output_single(filter, increment, start);
  else if (channels == 2) calc_output_stereo(filter, increment, start, 1., output);
  else if (channels == 4) calc_output_quad(filter, increment, start, 1., output);
  else if (channels == 6) calc_output_hex(filter, increment, start, 1., output);
  else calc_output_multi(filter, increment, start, channels, 1., output);
  for (int i=0;i<channels;i++) assert(output[i] == 4.0f);
  free(coeffs); free(filter);
  printf("endpoint channels=%d passed\n", channels);
  return 0;
}
