#!/usr/bin/python

from lxml.builder import E
import lxml.etree as ET
import os
import subprocess
import sys
import warnings

import matplotlib.pyplot as plt
import numpy as np
import pylab
import scipy
import scipy.signal
import scipy.io.wavfile

def calc_speedup_ratio(audio_file, speed):
    """Calculate adaptive speed-up ratio in the audio.

    Args:
        audio_file: path of the audio file.
        speed: desired speed specified by user.

    Returns:
        An array of Segment instances, which are segments (including its own
        speedup ratio) of this audio.
    """
    audio, fs = load_audio(audio_file)
    # list of tuple (start, end)
    syllable_times = detect_syllables(audio, fs)
    # calcDensity() + calcDensityMedian() => list of density
    vote_density = calc_vote_density(syllable_times, audio, fs)
    # calcSegments() + mergeSegments() => list of segment's start point
    start_points = calc_segments(vote_density)
    # calculate syllable density of each segment
    syllable_density = calc_syllable_density(start_points, syllable_times)
    # list of ratio
    speedup_ratio = calc_ratios(start_points, syllable_density, speed, audio,
                                fs)
    # Create all segments.
    segments = []
    for i in xrange(1, len(start_points)):
        ratio = np.around(speedup_ratio[i - 1], decimals=2)
        s = Segment(float(start_points[i - 1]) / 1000,\
                    float(start_points[i]) / 1000,\
                    max(0.1, min(100, ratio)))
        segments.append(s)
    # (optional, used to lower memory requirement) quantize speedup ratio
    for s in segments:
        s.ratio = quantize_speedup_ratio(s.ratio)
    return segments

def quantize_speedup_ratio(ratio):
    """Return quantized speedup ratio as multiple of 0.25. e.g. 1.25, 3.00."""
    if ratio < 0.25:
        return 0.25
    return round(ratio * 4) / 4

def detect_syllables(audio, fs):
    """Detect syllables' timing from audio data.

    Args:
        audio: audio data.
        fs: sampling rate of the audio.

    Returns:
        An array of tuple (start_time, end_time) of detected syllables. The
        array is sorted chronologically by the occurence of syllables.
    """
    syllables = harma_batch(audio, fs)
    print len(syllables)
    syllable_times = []
    for s in syllables:
        start = np.amin(s.times)
        end = np.amax(s.times)
        syllable_times.append((start, end))
    return sorted(syllable_times)

def harma_batch(audio, fs):
    """Perform Harma in batch manner (shorter audio), so specgram() doesn't
    run out of memory."""
    STEP_SIZE = 5000000
    length = audio.shape[0]
    syllables = []
    for start in xrange(0, length, STEP_SIZE):
        end = min(start + STEP_SIZE, length)
        batch_syllables = harma(audio[start:end], fs)
        # Offset syllable time by batch's start time
        for s in batch_syllables:
            s.times = [t + float(start) / fs for t in s.times]
        syllables += batch_syllables
    return syllables

def harma(audio, fs):
    """Detect syllables by the Harma algorithm.

    Args:
        audio: audio data.
        fs: sampling rate of the audio.

    Returns:
        An array of Syllable instances, the detected syllables.
    """
    # Parameters for harma.
    nfft = 256
    window = np.kaiser(nfft, 0.5)
    minDB = 20
    noverlap = 128
    # Calculate spectrogram.
    mag, F, T, _ = pylab.specgram(audio, Fs=fs, window=window,
                                  noverlap=noverlap, NFFT=nfft,
                                  mode='magnitude')
    # Initialize segmentation parameters.
    syllables = []
    cutoff = None
    # Segment signals into syllables.
    while True:
        # Find the maximum remaining magnitude in the spectrogram.
        freqMax = np.amax(mag, axis=0)
        freqIndex = mag.argmax(axis=0)
        argMax = np.amax(freqMax)
        segmentIndex = freqMax.argmax()
        # Clear temp variables for this iteration.
        times = np.zeros(shape=0)
        segments = np.zeros(shape=0, dtype=np.int)
        freqs = np.zeros(shape=0)
        amps = np.zeros(shape=0)
        # Setup temp variables with initial values.
        segments = np.append(segments, segmentIndex)
        times = np.append(times, T[segmentIndex])
        freqs = np.append(freqs, F[freqIndex[segmentIndex]])
        amps = np.append(amps, 20 * np.log10(argMax))
        # Check if this is the first iteration,
        # if so store the cutoff value for the loop.
        if cutoff is None:
            cutoff = amps[0] - minDB
        # Is it time to stop looking for syllables?
        if amps[0] < cutoff:
            break
        minAmp = amps[0] - minDB
        i = 0
        # Look for all the values less than t with a high enough amplitude.
        t = segmentIndex
        while t > 0 and amps[i] >= minAmp:
            t -= 1
            i += 1
            segments = np.append(segments, t)
            times = np.append(times, T[t])
            freqs = np.append(freqs, F[freqIndex[t]])
            with warnings.catch_warnings(): # suppress divide-by-zero warning
                warnings.simplefilter('ignore')
                amps = np.append(amps, 20 * np.log10(freqMax[t]))
        # Remove the last index because it did not meet criteria.
        if i > 0:
            segments = np.delete(segments, i)
            times = np.delete(times, i)
            freqs = np.delete(freqs, i)
            amps = np.delete(amps, i)
            i -= 1
        # Look for all the values less than t with a high enough amplitude.
        while t < freqIndex.size - 1 and amps[i] >= minAmp:
            t += 1
            i += 1
            segments = np.append(segments, t)
            times = np.append(times, T[t])
            freqs = np.append(freqs, F[freqIndex[t]])
            with warnings.catch_warnings(): # suppress divide-by-zero warning
                warnings.simplefilter('ignore')
                amps = np.append(amps, 20 * np.log10(freqMax[t]))
        # Remove the last index because it did not meet criteria.
        if i > 0:
            segments = np.delete(segments, i)
            times = np.delete(times, i)
            freqs = np.delete(freqs, i)
            amps = np.delete(amps, i)
            i -= 1
        # Store syllable parameters in struct. (irrelevant things are ignored)
        syllable = Syllable(times)
        syllables.append(syllable)
        # Clear the magnitudes for this syllable so that it is not found again.
        mag[:, segments] = 0
    return syllables

def calc_vote_density(syllable_times, audio, fs):
    """Calculate the vote density given timing of syllables.

    Args:
        syllable_times: an array of tuple (start, end), each syllable's timing.
        audio: audio data.
        fs: sampling rate of the audio.

    Returns:
        An array of the vote density, having the same length as the audio data.
        The vote density is median-filtered.
    """
    # Compute density by voting.
    voteWindow = 0.3 # in second
    vote_density = np.zeros(int(float(audio.size) / fs * 1000), # in ms
                            dtype=np.uint32)
    for start, end in syllable_times:
        vote_start = int(np.floor((start - voteWindow) * 1000)) - 1
        vote_end = int(np.floor((end + voteWindow) * 1000))
        if vote_start < 0:
            vote_start = 0
        if vote_end > vote_density.size:
            vote_end = vote_density.size
        for i in xrange(vote_start, vote_end):
            vote_density[i] += 1
    # Median filtering
    window_size = 151
    return scipy.signal.medfilt(vote_density, window_size)

def calc_segments(vote_density):
    """Calculate segments from the vote density.

    Args:
        vote_density: An array of vote density (same length as the audio data).

    Returns:
        An array of segment's start time.
    """
    # Calculate the splitting points of segments.
    seg_points = np.array([0], dtype=np.uint32)
    in_valley = True
    valley_start = 0
    for m in xrange(1, vote_density.size):
        if vote_density[m - 1] < vote_density[m]:
            if in_valley: # valley ends
                seg_points = np.append(seg_points, [valley_start, m])
            in_valley = False
        elif vote_density[m - 1] > vote_density[m]:
            valley_start = m
            in_valley = True
    # Make sure 'seg_points' has the end point of 'vote_density'.
    if seg_points[-1] != vote_density.size:
        seg_points = np.append(seg_points, vote_density.size - 1);
    # Merge splitting points to create segment start points.
    min_segment_length = 400 # in ms
    start_points = np.array([0])
    seg_start = seg_points[0]
    for m in xrange(1, seg_points.size):
        if seg_points[m] - seg_start > min_segment_length:
            start_points = np.append(start_points, seg_points[m])
            seg_start = seg_points[m]
    return start_points

def calc_ratios(start_points, syllable_density, speed, audio, fs):
    """Calculate speedup ratio of segments.

    Args:
        start_points: an array of segment's start time.
        syllable_density: an array of segment's syllable density.
        speed: desired speed specified by user.
        audio: audio data.
        fs: sampling rate of the audio.

    Returns:
        An array of each segment's speedup ratio.
    """
    pause_time = 150 # desired pause time (in ms)
    avg_density = np.mean(syllable_density)
    # Pause count and speak time.
    pause_count = 0
    speak_time = 0
    for m in xrange(1, start_points.size):
        seg_start, seg_end = start_points[m - 1], start_points[m]
        if syllable_density[m - 1] == 0 and seg_end - seg_start > pause_time:
            pause_count += 1
        else:
            ratio = avg_density / syllable_density[m - 1]
            speak_time += float(seg_end - 1 - seg_start) / ratio
    # Calculate desired ratio.
    audio_length = float(audio.size) / fs * 1000 # audio length in ms.
    expected_time = audio_length / speed
    desired_ratio = speak_time / (expected_time - pause_count * pause_time)
    # speed up
    speedup_ratio = np.zeros(0)
    for m in xrange(1, start_points.size):
        seg_start, seg_end = start_points[m - 1], start_points[m]
        if syllable_density[m - 1] == 0 and seg_end - seg_start > pause_time:
            ratio = (seg_end - 1 - seg_start) / pause_time
        else:
            ratio = avg_density * desired_ratio / syllable_density[m - 1]
        speedup_ratio = np.append(speedup_ratio, ratio)
    return speedup_ratio

def calc_syllable_density(start_points, syllable_times):
    """Calculate the syllable density of each segment.

    Args:
        start_points: an array of segment's start time.
        syllable_times: an array of tuple (start, end), each syllable's timing.

    Returns:
        An array of syllable density of each segment.
    """
    syllable_density = np.zeros(len(start_points) - 1)
    index = 0
    for i in xrange(1, len(start_points)):
        count = 0
        while index < len(syllable_times) and\
                1000 * syllable_times[index][0] < start_points[i]:
            index += 1
            count += 1
        syllable_density[i - 1] = float(count) /\
                                  (start_points[i] - start_points[i - 1])
    return syllable_density

def load_audio(audio_file):
    """Load audio data given the audio file path.

    Args:
        audio_file: path of the audio file.

    Returns:
        A tuple of (data, sample_rate). The data is normalized to [-1, 1].
    """
    # Suppress the warning from scipy loading wav audio_file.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        sample_rate, audio = scipy.io.wavfile.read(audio_file)
    mono = audio[:, 0] # only the first channel is used
    normalized = pcm2float(mono, 'float32')
    return normalized, sample_rate

def pcm2float(sig, dtype='float64'):
    """Convert WAV signal from integer to float point value with range [-1, 1].

    (Excerpted from mgeier on Github).
    """
    sig = np.asarray(sig)
    if sig.dtype.kind != 'i':
        raise TypeError("'sig' must be an array of signed integers")
    dtype = np.dtype(dtype)
    if dtype.kind != 'f':
        raise TypeError("'dtype' must be floating point type")

    # Note that 'min' has a greater (by 1) absolute value than 'max'!
    # Therefore, we use '-min' here to avoid clipping.
    return sig.astype(dtype) / dtype.type(-np.iinfo(sig.dtype).min)

def gen_audio_clips(audio_file, segments):
    """Use command 'sox' to generate audio clips with adaptive speed
        corresponding to each video segment.

    Args:
        audio_file: path of the audio file.
        segments: an array of segments.

    Returns:
        An array of paths to the audio clip of its video segment.
    """
    base_name, extension = os.path.splitext(audio_file)
    audio_end = segments[-1].end
    audio_clips = []
    for i in xrange(len(segments)):
        s = segments[i]
        output_file = base_name + '_' + str(i) + extension
        audio_clips.append(os.path.split(output_file)[1])
        # preserve 1 second at the end of each segment,
        # for MELT (the video editor) to grab frames.
        end = s.end + 1
        if end > audio_end:
            end = audio_end
        # Run 'sox' to generate audio clips.
        p = subprocess.call(['sox', audio_file, output_file,\
                             'trim', '%.3f' % s.start, '=%.3f' % end,\
                             'tempo', '-s', '%.2f' % s.ratio])
    return audio_clips

def render(video_file, sh_script_path, mlt_script_path, target_path,\
           segments, audio_clips):
    """Generate render scripts, then render the output video.

    Args:
        video_file: path to the input video.
        sh_script_path: path to the BASH script to be generated.
        mlt_script_path: path to the MLT script to be generated.
        target_path: path to the output video.
        segments: an array of segments.
        audio_clips: an array of paths to the audio clip of its video segment.
    """
    # Generate BASH script.
    bash_script = gen_bash_script(mlt_script_path, target_path)
    with open(sh_script_path, 'w') as f:
        f.write(bash_script)
    # Generate melt script (XML).
    mlt_script = gen_melt_script(video_file, segments, audio_clips)
    with open(mlt_script_path, 'w') as f:
        f.write("<?xml version='1.0' encoding='utf-8'?>\n")
        f.write(mlt_script)
    # Render.
    #subprocess.call(['bash', sh_script_path])

def gen_bash_script(mlt_script_path, target_path):
    """Generate and returns the BASH script used to render output video.

    Args:
        mlt_script_path: path to the MLT script to be generated.
        target_path: path to the output video.

    Returns:
        Content of the BASH script.
    """
    mlt_script_abs_path = os.path.abspath(mlt_script_path)
    target_abs_path = os.path.abspath(target_path)
    s = ''
    s += '#! /bin/sh\n'
    s += 'SOURCE="' + mlt_script_abs_path + '"\n'
    s += 'TARGET="' + target_abs_path + '"\n'
    s += 'RENDERER="/usr/bin/kdenlive_render"\n'
    s += 'MELT="/usr/bin/melt"\n'
    s += 'PARAMETERS="-pid:24332 $MELT hdv_1080_50i avformat - '\
         '$SOURCE $TARGET f=mp4 acodec=libmp3lame ab=128k ar=44100 '\
         'vcodec=mpeg4 minrate=0 vb=12000k aspect=@16/9 mbd=2 trellis=1 '\
         'mv4=1 pass=1 threads=1 real_time=-1"\n'
    s += '$RENDERER $PARAMETERS\n'
    return s

def gen_melt_script(video_file, segments, audio_clips):
    """Generate and returns the MLT script used to render output video.

    Args:
        video_file: path to the input video.
        segments: an array of segments.
        audio_clips: an array of paths to the audio clip of its video segment.

    Returns:
        Content of the MLT script.
    """
    # Some useful info.
    frame_rate, frame_length = get_video_profiles(video_file)
    video_time = segments[-1].end # video time in second.
    root_dir = os.path.dirname(os.path.abspath(video_file))
    video_base_name = os.path.basename(video_file)
    # XML elements
    title = 'Speeda'
    mlt = E('mlt', {'title': title, 'version': '0.9.0', 'root': root_dir,\
                    'LC_NUMERIC': 'en_US.UTF-8'})
    audio_track = E('playlist', {'id': 'playlist3'}) # Audio track.
    video_track = E('playlist', {'id': 'playlist5'}) # Video track.
    # Add clips to audio/video track.
    video_ids = set()
    final_frame_length = 0
    for i in xrange(len(segments)):
        s = segments[i]
        video_producer_id = 'slowmotion:2:%.2f' % s.ratio
        producer_frame_length = int(frame_length / s.ratio)
        video_resource = video_base_name + '?%.2f' % s.ratio
        start_frame = int(s.start / video_time * producer_frame_length)
        end_frame = int(s.end / video_time * producer_frame_length) - 1
        clip_frame_length = end_frame - start_frame + 1
        final_frame_length += clip_frame_length
        # Video producer.
        if video_producer_id not in video_ids:
            # only create one <producer> node if same speed-up ratio.
            video_ids.add(video_producer_id)
            video_producer = create_producer_node(video_producer_id,\
                                                  producer_frame_length,
                                                  video_resource, is_video=True)
            mlt.append(video_producer)
        # Audio producer.
        audio_producer_id = 'audio_%d' % i
        audio_resource = audio_clips[i]
        audio_producer = create_producer_node(audio_producer_id,\
                                              clip_frame_length,
                                              audio_resource, is_video=False)
        mlt.append(audio_producer)
        # Video clip.
        video_track.append(E('entry', {'in': str(start_frame),
                                       'out': str(end_frame),
                                       'producer': video_producer_id}))
        # Audio clip.
        audio_track.append(E('entry', {'in': '0',
                                       'out': str(clip_frame_length - 1),
                                       'producer': audio_producer_id}))
    mlt.append(E('playlist', {'id': 'playlist1'})) # Empty track.
    mlt.append(E('playlist', {'id': 'playlist2'})) # Empty track.
    mlt.append(audio_track)
    mlt.append(E('playlist', {'id': 'playlist4'})) # Empty track.
    mlt.append(video_track)
    # Tractor.
    tractor = E('tractor', {'title': title, 'global_feed': '1',
                            'in': '0', 'out': str(final_frame_length - 1),
                            'id': 'maintractor'})
    tractor.append(E('track', {'hide': 'video', 'producer': 'playlist1'}))
    tractor.append(E('track', {'hide': 'video', 'producer': 'playlist2'}))
    tractor.append(E('track', {'producer': 'playlist3'}))
    tractor.append(E('track', {'producer': 'playlist4'}))
    tractor.append(E('track', {'producer': 'playlist5'}))
    mlt.append(tractor)
    return ET.tostring(mlt, pretty_print=True)

def create_producer_node(producer_id, frame_length, resource, is_video):
    """Create a <producer> node of MLT script. An example is given below.

    <producer out="230" id="slowmotion:2:13.00" in="0">
      <property name="mlt_type">producer</property>
      <property name="length">231</property>
      <property name="resource">ai_short.mp4?13.00</property>
      <property name="mlt_service">framebuffer</property>
    </producer>

    Args:
        producer_id: ID of the <producer> node.
        frame_length: the frame length of the producer.
        resource: path to the resource (may include speed-up info).
        is_video: True if the producer is video. False if audio.

    Returns:
        The created <producer> node, an instance of lxml.builder.E.
    """
    p = E('producer', {'in': '0',
                       'out': str(frame_length - 1),
                       'id': producer_id})
    p.append(E('property', {'name': 'mlt_type'}, 'producer'))
    p.append(E('property', {'name': 'length'}, str(frame_length)))
    p.append(E('property', {'name': 'resource'}, resource))
    p.append(E('property', {'name': 'mlt_service'},
               'framebuffer' if is_video else 'avformat'))
    return p

def get_video_profiles(video_file):
    """Returns profile (info) of a video.

    Args:
        video_file: path to the video.

    Returns:
        A tuple of (frame_rate, frame_length).
    """
    p = subprocess.Popen(['melt', video_file, '-consumer', 'xml'],
                         stdout=subprocess.PIPE)
    stdout, stderr = p.communicate()
    # Parse XML.
    root = ET.fromstring(stdout)
    # Get useful info.
    frame_rate = int(root.find('profile').get('frame_rate_num'))
    for property_tag in root.find('producer'):
        if property_tag.get('name') == 'length':
            frame_length = int(property_tag.text)
    return frame_rate, frame_length

def exp_harma():
    """Small experiment on Harma parameters."""
    audio_file = 'playground/ai_short/ai_short.wav'
    audio, fs = load_audio(audio_file)
    syllable_times = detect_syllables(audio, fs)
    start_time = [start for (start, end) in syllable_times]
    end_time = [end for (start, end) in syllable_times]

    plt.close('all')
    plt.figure()
    plt.plot(start_time, [1] * len(start_time), 'ro', markersize=1)
    plt.plot(end_time, [0.9] * len(end_time), 'ro', markersize=1)
    plt.axis([0, max(end_time), 0, 2])
    plt.title('Detected syllables (start: level 1, end: level 0.9)')
    plt.show()

# A syllable detected by Harma. Only keep relevant info here.
class Syllable:
    def __init__(self, times):
        self.times = times

# A segment with start time, end time, and its speed-up ratio.
# Time is in second. e.g. start = 1.234 means 1.234 second.
class Segment:
    def __init__(self, start, end, ratio):
        self.start = start
        self.end = end
        self.ratio = ratio

    def __str__(self):
        return '(start, end, ratio) = (%.2f, %.2f, %.2f)' % (\
                self.start, self.end, self.ratio)

if __name__ == '__main__':
    """Main function of Speeda."""
    #exp_harma()
    #sys.exit(0)

    audio_file = 'playground/ai/ai.wav' # TODO extract from video
    video_file = 'playground/ai/ai.mp4'
    sh_script_path = 'playground/ai/test.sh'
    mlt_script_path = sh_script_path + '.mlt'
    target_path = 'playground/ai/test.mp4'

    segments = calc_speedup_ratio(audio_file, 1)
    audio_clips = gen_audio_clips(audio_file, segments)
    render(video_file, sh_script_path, mlt_script_path, target_path,\
           segments, audio_clips)
