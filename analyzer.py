import essentia.standard as es

class AudioAnalyzer:
    def __init__(self):
        # Static map for Standard to Camelot conversion
        self.camelot_map = {
            # Major Keys (B)
            "B Major": "1B", "F# Major": "2B", "Gb Major": "2B", "Db Major": "3B", "C# Major": "3B",
            "Ab Major": "4B", "G# Major": "4B", "Eb Major": "5B", "D# Major": "5B", "Bb Major": "6B", "A# Major": "6B",
            "F Major": "7B", "C Major": "8B", "G Major": "9B", "D Major": "10B", "A Major": "11B", "E Major": "12B",
            
            # Minor Keys (A)
            "Ab Minor": "1A", "G# Minor": "1A", "Eb Minor": "2A", "D# Minor": "2A", "Bb Minor": "3A", "A# Minor": "3A",
            "F Minor": "4A", "C Minor": "5A", "G Minor": "6A", "D Minor": "7A", "A Minor": "8A", "E Minor": "9A",
            "B Minor": "10A", "F# Minor": "11A", "Gb Minor": "11A", "Db Minor": "12A", "C# Minor": "12A"
        }

    def _get_camelot_key(self, key: str, scale: str) -> str:
        """Helper to convert standard key to Camelot notation."""
        # Essentia returns keys like 'C', 'Bb' and scales like 'major', 'minor'
        formatted_key = f"{key} {scale.capitalize()}"
        return self.camelot_map.get(formatted_key, "Unknown")

    def analyze_file(self, file_path: str) -> dict:
        """
        Analyzes an audio file for BPM, Key, and Silence.
        Returns a dictionary with analysis results.
        """
        try:
            # 1. Load Audio
            # Resample to 44.1kHz mono as per constitution/requirements
            loader = es.MonoLoader(filename=file_path, sampleRate=44100)
            audio = loader()

            # 2. Silence Removal
            # Initialize algorithms fresh for each file to avoid state caching issues
            silence_remover = es.StartStopSilence(threshold=-60)
            
            # Get start and end times of non-silent audio
            start_time, end_time = silence_remover(audio)
            
            # Convert to samples
            start_sample = int(start_time * 44100)
            end_sample = int(end_time * 44100)
            
            # Truncate audio if valid range found
            if end_sample > start_sample:
                audio = audio[start_sample:end_sample]

            # 3. BPM Detection
            # RhythmExtractor2013 returns: bpm, ticks, confidence, estimates, bpmIntervals
            rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
            bpm, _, beats_confidence, _, _ = rhythm_extractor(audio)

            # 4. Key Detection
            # KeyExtractor returns: key, scale, strength
            key_extractor = es.KeyExtractor()
            key, scale, key_strength = key_extractor(audio)

            # 5. Format Results
            result = {
                "bpm": round(bpm, 1),
                "bpm_confidence": float(beats_confidence),
                "key_standard": f"{key} {scale.capitalize()}",
                "key_camelot": self._get_camelot_key(key, scale),
                "key_confidence": float(key_strength),
                "duration": len(audio) / 44100.0
            }
            
            return result

        except Exception as e:
            # In a production service, we might want to log this properly
            raise RuntimeError(f"Analysis failed for {file_path}: {str(e)}")
