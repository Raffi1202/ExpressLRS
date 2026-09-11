#!/usr/bin/env python3
"""Host regression test for LR1121 TX radio masks.

Compiles the production TXnb method with a recording HAL because the native
PlatformIO environment excludes LR1121Driver. This checks command routing and
payload selection, not hardware timing, RSSI thresholds or RF compliance.
Run with Python 3 and a C++11 compiler; no Python packages are required.
"""

import argparse
from pathlib import Path
import subprocess
import tempfile

PREFIX = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#define ICACHE_RAM_ATTR
#define WORD_ALIGNED_ATTR
using SX12XX_Radio_Number_t = unsigned;
constexpr unsigned SX12XX_Radio_NONE=0, SX12XX_Radio_1=1, SX12XX_Radio_2=2, SX12XX_Radio_All=3;
constexpr int GPIO_PIN_NSS_2=1, UNDEF_PIN=-1;
constexpr unsigned LR11XX_RADIO_WRITE_BUFFER8_SET_TX=0x55;
struct Event { unsigned radio; uint8_t payload; };
struct Hal { std::vector<Event> events;
 void WriteCommand(unsigned cmd,const uint8_t* buf,unsigned length,unsigned radio) {
  if(cmd==LR11XX_RADIO_WRITE_BUFFER8_SET_TX) events.push_back({radio,buf[0]});
 }};
struct Codec { void encode(uint8_t* dst,const uint8_t* src,unsigned n) { std::memcpy(dst,src,n); }};
struct LR1121Driver {
 unsigned transmittingRadio=0; int fallBackMode=0; uint8_t PayloadLength=8;
 Codec c; Codec* codec=&c; Hal hal;
 void SetMode(int,unsigned) {}
 void TXnb(uint8_t*,bool,uint8_t*,SX12XX_Radio_Number_t);
};
'''

SUFFIX = r'''
int main() {
 unsigned failures=0;
 for(unsigned mask=0;mask<4;mask++) for(bool split:{false,true}) {
  LR1121Driver d; uint8_t a[8]={0xA1},b[8]={0xB2};
  d.TXnb(a,split,b,mask); unsigned actual=0; bool payload_ok=true;
  for(auto event:d.hal.events) {
   actual|=event.radio;
   payload_ok &= event.payload == (split && event.radio==2 ? 0xB2 : 0xA1);
  }
  bool pass=actual==mask && payload_ok;
  failures += !pass;
  std::printf("mask=%u split=%u actual_tx_mask=%u payload_ok=%u %s\n",mask,split,actual,payload_ok,pass?"PASS":"FAIL");
 }
 return failures?1:0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=Path(__file__).resolve().parents[2] / "lib/LR1121Driver/LR1121.cpp")
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8")
    start = source.index("void ICACHE_RAM_ATTR LR1121Driver::TXnb(")
    end = source.index("\ninline void ICACHE_RAM_ATTR LR1121Driver::DecodeRssiSnr", start)
    method = source[start:end]
    with tempfile.TemporaryDirectory(prefix="elrs-lr1121-mask-") as temp:
        cpp = Path(temp) / "test.cpp"
        exe = Path(temp) / "test"
        cpp.write_text(PREFIX + method + SUFFIX, encoding="utf-8")
        subprocess.run([args.cxx, "-std=c++11", "-O2", str(cpp), "-o", str(exe)], check=True)
        return subprocess.run([str(exe)], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
