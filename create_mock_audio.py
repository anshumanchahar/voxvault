import wave
import struct
import math

# Create a simple 1-second 440Hz tone WAV file
sample_rate = 44100
duration = 1.0
frequency = 440.0

with wave.open('app/static/mock_audio.wav', 'w') as wav_file:
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(sample_rate)
    
    for i in range(int(sample_rate * duration)):
        value = int(32767 * 0.3 * math.sin(2 * math.pi * frequency * i / sample_rate))
        data = struct.pack('<h', value)
        wav_file.writeframes(data)

print('Created mock_audio.wav')