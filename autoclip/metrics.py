"""Read-only composition from actual encoded timeline seconds, not source lengths."""


def audio_mix(timeline):
    seconds = {'narration': 0.0, 'original': 0.0}
    opening_mode = timeline[0]['mode'] if timeline else None
    opening_seconds = 0.0
    in_opening = True
    for beat in timeline:
        length = max(0, beat['output_end'] - beat['output_start'])
        seconds[beat['mode']] += length
        if beat['mode'] != opening_mode:
            in_opening = False
        if in_opening:
            opening_seconds += length
    total = sum(seconds.values())
    return {**{mode + '_seconds': round(value, 3) for mode,value in seconds.items()},
            'original_ratio': round(seconds['original']/total, 4) if total else 0,
            'opening_mode': opening_mode, 'opening_seconds': round(opening_seconds, 3)}
