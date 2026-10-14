"""Bounds guard for libsamplerate CVE-2017-7697 with the legacy buffer schedule.

The upstream -2 half-length fix also changes the internal buffer geometry.
Keep the legacy length and instead prevent index+1 at the final coefficient.
Every other interpolation expression and the coefficient tables stay unchanged.
Upstream advisory fix: https://github.com/libsndfile/libsamplerate/commit/c3b66186656de44da18b7058aec099dbe782dd0b
"""
import hashlib
from pathlib import Path
p = Path('/samplerate/src/src_sinc.c')
source = p.read_text()
if hashlib.sha256(source.encode()).hexdigest() != 'fab009e1bc8e261c92360d395e2d197148bfb97324a9cc60f2e4d3057225b6a3':
    raise SystemExit('Unexpected pinned libsamplerate sinc source')
before = 'icoeff = filter->coeffs [indx] + fraction * (filter->coeffs [indx + 1] - filter->coeffs [indx]) ;'
after = 'icoeff = (indx < filter->coeff_half_len) ? filter->coeffs [indx] + fraction * (filter->coeffs [indx + 1] - filter->coeffs [indx]) : filter->coeffs [indx] ;'
if source.count(before) != 10:
    raise SystemExit('Unexpected libsamplerate coefficient interpolation sites')
p.write_text(source.replace(before, after))
print('Applied libsamplerate coefficient endpoint bounds guards')
