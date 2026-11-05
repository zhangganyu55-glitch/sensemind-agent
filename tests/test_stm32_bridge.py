from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from guardian.llm import LLMSettings
from guardian.main import create_app
from guardian.stm32_bridge import CounterTracker, DeviceReadError, Snapshot, decode_snapshot, telemetry_from_snapshot


ADDRESS = 0x200003d0


def snapshot(**changes):
    return replace(Snapshot(counter=100, heart_rate=78.9, finger=True, hr_valid=True,
                            weak=False, spo2=99, spo2_valid=True, overflow=0), **changes)


def memory_dump(words, address=ADDRESS):
    return '\n'.join(f'0x{address+i*4:08x}: ' + ' '.join(f'{w:08x}' for w in words[i:i+4])
                     for i in range(0, len(words), 4))


def words():
    values = [0] * 14
    values[0], values[4] = 100, 789 | (1 << 16) | (1 << 24)
    values[8] = (99 << 16) | (1 << 24)
    return values


def test_decodes_exact_firmware_layout():
    assert decode_snapshot(memory_dump(words()), ADDRESS) == snapshot()


@pytest.mark.parametrize('text', [memory_dump(words()[:13]), memory_dump(words(), ADDRESS+4),
                                  memory_dump(words()).replace('00000064', 'bad-word'), 'Error: target disconnected'])
def test_rejects_incomplete_or_misaligned_memory(text):
    with pytest.raises(DeviceReadError):
        decode_snapshot(text, ADDRESS)


@pytest.mark.parametrize('position,value', [(4, 3000 | (1 << 16) | (1 << 24)),
                                          (4, 789 | (2 << 16) | (1 << 24)),
                                          (8, (101 << 16) | (1 << 24))])
def test_rejects_invalid_fields(position, value):
    data = words()
    data[position] = value
    with pytest.raises(DeviceReadError):
        decode_snapshot(memory_dump(data), ADDRESS)


def test_counter_first_live_stalled_reset_and_wrap():
    tracker = CounterTracker()
    assert tracker.observe(100, 0) == ('pending', True)
    assert tracker.observe(200, 1) == ('live', True)
    assert tracker.observe(200, 2) == ('pending', False)
    assert tracker.observe(200, 4) == ('stalled', False)
    assert tracker.observe(5, 5) == ('pending', True)
    assert tracker.observe(105, 6) == ('live', True)
    tracker = CounterTracker()
    tracker.observe(0xfffffff0, 0)
    assert tracker.observe(84, 1) == ('live', True)


def test_live_packet_uses_actual_values_without_fabricated_motion():
    packet = telemetry_from_snapshot('stm32_f103_001', snapshot(), 'live')
    assert packet.source == 'stm32'
    assert packet.heart_rate == 78.9 and packet.spo2 == 99
    assert packet.reading_valid and packet.signal_quality == 'good'
    assert packet.acc_x is None and packet.gyro_x is None
    assert packet.device_status.sample_counter == 100
    assert packet.device_status.spo2_method == 'firmware_estimate'


@pytest.mark.parametrize('status', ['pending', 'stalled', 'disconnected'])
def test_stale_or_disconnected_memory_never_becomes_new_valid_measurement(status):
    packet = telemetry_from_snapshot('stm32_f103_001', snapshot(), status)
    assert packet.heart_rate is None and packet.spo2 is None
    assert packet.reading_valid is False and packet.signal_quality == 'unknown'
    assert packet.device_status.sampling_status == status


@pytest.mark.parametrize('data', [snapshot(finger=False), snapshot(weak=True)])
def test_no_finger_or_weak_signal_does_not_upload_values_as_valid(data):
    packet = telemetry_from_snapshot('stm32_f103_001', data, 'live')
    assert packet.heart_rate is None and packet.spo2 is None
    assert not packet.reading_valid and packet.signal_quality == 'poor'


def test_partial_validity_is_per_measurement():
    packet = telemetry_from_snapshot('stm32_f103_001', snapshot(hr_valid=False), 'live')
    assert packet.heart_rate is None and packet.spo2 == 99 and packet.reading_valid
    packet = telemetry_from_snapshot('stm32_f103_001', snapshot(spo2_valid=False), 'live')
    assert packet.heart_rate == 78.9 and packet.spo2 is None and packet.reading_valid


def test_bridge_packets_persist_and_real_rules_stay_disabled(tmp_path):
    c = TestClient(create_app(str(tmp_path/'bridge.db'), allow_real_rules=False, llm_settings=LLMSettings()))
    base = datetime.now(timezone.utc) - timedelta(seconds=10)
    for index, status in enumerate(('pending', 'live', 'disconnected')):
        packet = telemetry_from_snapshot('stm32_f103_001', snapshot(), status)
        packet.timestamp = base + timedelta(seconds=index)
        response = c.post('/v1/telemetry', json=packet.model_dump(mode='json'))
        assert response.status_code == 200
        assert response.json()['evaluation'] == 'real_data_rules_disabled'
    state = c.get('/v1/live/status?device_id=stm32_f103_001').json()
    assert state['latest']['device_status']['sampling_status'] == 'disconnected'
    assert state['latest']['heart_rate'] is None and not state['latest']['reading_valid']
    history = c.get('/v1/history?device_id=stm32_f103_001').json()
    assert history[1]['heart_rate'] == 78.9
    assert c.get('/v1/events?device_id=stm32_f103_001').json() == []
