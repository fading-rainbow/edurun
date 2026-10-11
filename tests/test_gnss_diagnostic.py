from pathlib import Path


def test_gnss_diagnostic_reads_real_callback_and_cleans_up():
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "android/diagnostic/src/com/edurun/diagnostic/DiagnosticActivity.java"
    ).read_text("utf-8")
    assert "onSatelliteStatusChanged(GnssStatus status)" in source
    assert "satellitesTracked = status.getSatelliteCount()" in source
    assert "status.usedInFix(i)" in source
    assert "manager.registerGnssStatusCallback(gnssCallback, handler)" in source
    assert "manager.unregisterGnssStatusCallback(gnssCallback)" in source
    assert 'gnss.put("satellites_used_in_fix", satellitesUsedInFix)' in source


def test_nmea_summary_uses_received_fields_without_saving_raw_sentences():
    root = Path(__file__).resolve().parents[1]
    source = (
        root / "android/diagnostic/src/com/edurun/diagnostic/DiagnosticActivity.java"
    ).read_text("utf-8")
    assert "onNmeaMessage(String message, long timestamp)" in source
    assert "rmcDate = fields[9]" in source
    assert "ggaSatellites = fields[7]" in source
    assert "manager.removeNmeaListener(nmeaListener)" in source
    assert 'nmea.put("rmc_date", rmcDate)' in source
    assert 'nmea.put("message", message)' not in source
