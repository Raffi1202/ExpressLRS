#!/usr/bin/env python3
"""Exercise the production Gemini merger with the real Stubborn transport.

The host harness substitutes radio reception and packet scheduling. It tests
payload integrity and ACK/retry behaviour, not RF timing, LBT thresholds or GNSS.
Each scenario runs in a fresh process to isolate the merger's static buffers.
"""

import argparse
from pathlib import Path
import subprocess
import tempfile


PREFIX = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "telemetry_protocol.h"
#include "stubborn_sender.h"
#include "stubborn_receiver.h"
#define ICACHE_RAM_ATTR
#define PACKED __attribute__((packed))
constexpr unsigned SX12XX_Radio_1 = 1;
constexpr unsigned SX12XX_Radio_2 = 2;
struct RadioStub {
    unsigned processing = 1;
    bool hasSecondRadioGotData = false;
    unsigned GetProcessingPacketRadio() { return processing; }
} Radio;
bool gemini = true;
bool inGeminiMode() { return gemini; }
StubbornSender DataUlSender;
StubbornReceiver DataDlReceiver;
StubbornSender remoteSender;
'''

SUFFIX = r'''
int main(int argc, char **argv)
{
    if (argc != 5) return 2;
    const bool full = std::atoi(argv[1]);
    const unsigned scenario = std::atoi(argv[2]);
    const unsigned firstRadio = std::atoi(argv[3]);
    const unsigned ratio = std::atoi(argv[4]);
    const unsigned large = full ? ELRS8_DATA_DL_BYTES_PER_CALL : ELRS4_DATA_DL_BYTES_PER_CALL;
    const unsigned small = large - sizeof(OTA_LinkStats_s);
    const unsigned maxIndex = full ? ELRS8_DATA_DL_MAX_PACKAGES : ELRS4_DATA_DL_MAX_PACKAGES;
    uint8_t expected[65], received[65] = {};
    for (unsigned i = 0; i < sizeof(expected); ++i) expected[i] = 17 + 3 * i;
    DataDlReceiver.setMaxPackageIndex(maxIndex);
    DataDlReceiver.SetDataToReceive(received, sizeof(received));
    remoteSender.setMaxPackageIndex(maxIndex);
    remoteSender.SetDataToTransmit(expected, sizeof(expected));
    // Keep timeout accounting consistent with one telemetry opportunity per ratio.
    remoteSender.UpdateTelemetryRate(150, ratio, 1);
    gemini = scenario != 5;
    unsigned slot = 0;
    for (unsigned tick = 1; tick <= ratio * 300; ++tick)
    {
        if (tick % ratio != 0)
        {
            // The RC uplink carries the downlink confirmation bit.
            remoteSender.ConfirmCurrentPayload(DataDlReceiver.GetCurrentConfirm());
            continue;
        }
        unsigned width = large;
        unsigned mask = 3;
        if (scenario == 1 || scenario == 2)
        {
            // Opposite radio halves arrive on opposite packet types. The
            // package index is unchanged until the complete payload is ACKed.
            const unsigned firstWidth = scenario == 1 ? small : large;
            const unsigned nextWidth = scenario == 1 ? large : small;
            width = slot == 0 ? firstWidth : slot < 3 ? nextWidth : large;
            if (slot == 0 || slot == 2) mask = firstRadio;
            if (slot == 1) mask = 3 ^ firstRadio;
        }
        else if (scenario == 3)
        {
            // Same-sized halves may be combined across separate attempts.
            if (slot == 0) mask = firstRadio;
            if (slot == 1) mask = 3 ^ firstRadio;
        }
        else if (scenario == 4)
        {
            // Temporary complete loss, then one-band loss, then recovery.
            mask = slot < 3 ? 0 : slot < 8 ? firstRadio : 3;
        }
        else if (scenario == 5)
        {
            mask = firstRadio; // existing non-Gemini path
        }
        uint8_t span[2 * ELRS8_DATA_DL_BYTES_PER_CALL] = {};
        const uint8_t pi = remoteSender.GetCurrentPayload(span, gemini ? 2 * width : width);
        if (mask)
        {
            Radio.processing = mask == 3 ? firstRadio : mask;
            Radio.hasSecondRadioGotData = mask == 3;
            uint8_t *a = span;
            uint8_t *b = span + width;
            if (gemini && Radio.processing == 2) { a = span + width; b = span; }
            ProcessOtaDataDl(pi, pi, a, b, width, false);
        }
        if ((scenario == 1 || scenario == 2) && slot == 1 && DataDlReceiver.GetCurrentConfirm())
        {
            std::fprintf(stderr, "FAIL: mismatched payload widths acknowledged as a complete pair\n");
            return 1;
        }
        ++slot;
        if (DataDlReceiver.HasFinishedData())
        {
            if (std::memcmp(expected, received, sizeof(expected)))
            {
                std::fprintf(stderr, "FAIL: completed telemetry differs from original bytes\n");
                return 1;
            }
            // Deliver the final ACK so the remote sender completes as well.
            remoteSender.ConfirmCurrentPayload(DataDlReceiver.GetCurrentConfirm());
            if (remoteSender.IsActive()) return 1;
            return 0;
        }
    }
    std::fprintf(stderr, "FAIL: transfer did not recover\n");
    return 1;
}
'''


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "src/tx_main.cpp")
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8")
    start = source.index("void ICACHE_RAM_ATTR ProcessOtaDataDl(")
    end = source.index("\nstatic bool ICACHE_RAM_ATTR ProcessDownlinkPacket", start)
    ota = (root / "lib/OTA/OTA.h").read_text(encoding="utf-8")
    stats_start = ota.index("typedef struct {\n    uint8_t uplink_RSSI_1:7,")
    stats_end = ota.index("} PACKED OTA_LinkStats_s;", stats_start) + len("} PACKED OTA_LinkStats_s;")
    with tempfile.TemporaryDirectory(prefix="elrs-gemini-downlink-") as temp:
        cpp, exe = Path(temp) / "test.cpp", Path(temp) / "test"
        cpp.write_text(PREFIX + ota[stats_start:stats_end] + "\n" + source[start:end] + SUFFIX,
                       encoding="utf-8")
        subprocess.run([
            args.cxx, "-std=c++11", "-O2", "-Wall", "-Wextra",
            "-I" + str(root / "include"),
            "-I" + str(root / "lib/StubbornSender"),
            "-I" + str(root / "lib/StubbornReceiver"),
            str(cpp), str(root / "lib/StubbornSender/stubborn_sender.cpp"),
            str(root / "lib/StubbornReceiver/stubborn_receiver.cpp"),
            "-o", str(exe),
        ], check=True)
        failures = 0
        total = 0
        for full in (0, 1):
            for scenario in range(6):
                for first_radio in (1, 2):
                    for ratio in (2, 4, 8, 32):
                        result = subprocess.run(
                            [str(exe), str(full), str(scenario), str(first_radio), str(ratio)],
                            capture_output=True, text=True, check=False,
                        )
                        total += 1
                        if result.returncode:
                            failures += 1
                            print(f"full={full} scenario={scenario} first_radio={first_radio} "
                                  f"ratio=1:{ratio}: {result.stderr.strip()}")
        print(f"Gemini downlink: {total - failures}/{total} host scenarios passed")
        return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
