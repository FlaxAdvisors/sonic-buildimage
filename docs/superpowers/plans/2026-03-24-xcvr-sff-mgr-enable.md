# xcvr sff_mgr Enable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable SONiC's `sff_mgr` task so that PC5–PC8 QSFP28 modules on the Wedge 100S-32X receive correct byte 93 High Power Class Enable writes and come up `oper up` on insertion.

**Architecture:** Two atomic changes — add `"enable_xcvrd_sff_mgr": true` to `pmon_daemon_control.json` (tells xcvrd to start `SffManagerTask`), and remove the now-redundant `_init_power_override()` path from `sfp.py` (sff_mgr handles all power-class EEPROM writes correctly). All EEPROM writes still flow through `Sfp.write_eeprom()` → IPC → `wedge100s-i2c-daemon`, preserving the single-I2C-master constraint.

**Tech Stack:** Python 3, SONiC xcvrd/sff_mgr, JSON config, SSH deploy to `admin@192.168.88.12`.

---

## File Map

| File | Action | What changes |
|------|--------|--------------|
| `device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json` | Modify | Add `"enable_xcvrd_sff_mgr": true` |
| `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py` | Modify | Remove `_POWER_INIT_MTIME` dict (line 67), `_init_power_override()` method (lines 198–222), and call site `self._init_power_override(cached_data)` (line 130) |

No new files are created. No other files are touched.

---

## Task 1: Enable sff_mgr in pmon daemon config

**Files:**
- Modify: `device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json`

- [ ] **Step 1: Verify current content**

  ```bash
  cat device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json
  ```

  Expected: 4-key object with no `enable_xcvrd_sff_mgr` key.

- [ ] **Step 2: Add the sff_mgr flag**

  Replace the file content with:

  ```json
  {
      "skip_ledd": false,
      "skip_xcvrd": false,
      "skip_psud": false,
      "skip_thermalctld": false,
      "enable_xcvrd_sff_mgr": true
  }
  ```

- [ ] **Step 3: Verify JSON is valid**

  ```bash
  python3 -c "import json; json.load(open('device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json')); print('ok')"
  ```

  Expected: `ok`

- [ ] **Step 4: Commit**

  ```bash
  git add device/accton/x86_64-accton_wedge100s_32x-r0/pmon_daemon_control.json
  git commit -m "platform: wedge100s: enable xcvrd sff_mgr for PC5-8 high-power class init"
  ```

---

## Task 2: Remove _init_power_override from sfp.py

**Files:**
- Modify: `platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py`

The three removals are independent edits to the same file; apply them together.

- [ ] **Step 1: Confirm exact line numbers before editing**

  ```bash
  grep -n "_POWER_INIT_MTIME\|_init_power_override" \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py
  ```

  Expected output (line numbers must match; if they differ, adjust the edits below):
  ```
  67:_POWER_INIT_MTIME: dict = {}           # port → eeprom mtime when Power Override was last written
  130:        self._init_power_override(cached_data)
  198:    def _init_power_override(self, cached_data):
  214:        if _POWER_INIT_MTIME.get(self._port) == mtime:
  222:        _POWER_INIT_MTIME[self._port] = mtime
  ```

- [ ] **Step 2: Remove `_POWER_INIT_MTIME` dict (line 67)**

  Delete this line (it sits between the `_DOM_LAST_REFRESH` line and the blank line before `_wait_for_file`):

  ```python
  _POWER_INIT_MTIME: dict = {}           # port → eeprom mtime when Power Override was last written
  ```

  After removal, line 65–68 should read:
  ```python
  _DOM_CACHE_TTL      = 20              # seconds: max staleness per port
  _DOM_LAST_REFRESH   = [0.0] * NUM_SFPS  # monotonic timestamp of last live read

  def _wait_for_file(path, timeout_s):
  ```

- [ ] **Step 3: Remove the call site in `read_eeprom()` (line 130)**

  Delete this line (it sits just before the DOM refresh comment):

  ```python
          self._init_power_override(cached_data)
  ```

  After removal, the code around that area should read:
  ```python
          if cached_data is None:
              return None

          # Demand-driven lower-page refresh when TTL has expired.
          if offset < 128 and (time.monotonic() - _DOM_LAST_REFRESH[self._port]) > _DOM_CACHE_TTL:
  ```

  (No blank line should be left behind — remove both the call and any blank line that was there only as a separator before the next comment, if applicable. Check visually after the edit.)

- [ ] **Step 4: Remove `_init_power_override()` method (lines 198–222)**

  Delete the entire method from its docstring through its last line, inclusive:

  ```python
      def _init_power_override(self, cached_data):
          """Set Power Override (byte 93 bit 1) on first EEPROM read after insertion.

          SFF-8636 byte 129 bits 7-6 = 0b11 means Power Class 4+ (≥3.5 W).
          Without Power Override=1 these modules stay in a reduced-power idle
          state and the laser does not fire even with lpmode deasserted.

          Keyed on eeprom file mtime so re-insertion triggers a fresh write.
          Preserves CDR-control bits (3-2); clears Power Set (bit 0) so the
          module runs at full rated power.
          """
          cache_path = _I2C_EEPROM_CACHE.format(self._port)
          try:
              mtime = os.path.getmtime(cache_path)
          except OSError:
              return
          if _POWER_INIT_MTIME.get(self._port) == mtime:
              return

          if len(cached_data) >= 130 and ((cached_data[129] >> 6) & 0x03) == 0x03:
              byte93 = cached_data[93]
              if not (byte93 & 0x02):
                  self.write_eeprom(93, 1, bytearray([(byte93 | 0x02) & ~0x01]))

          _POWER_INIT_MTIME[self._port] = mtime
  ```

  The blank line before `def write_eeprom` that follows should be preserved.

- [ ] **Step 5: Verify no remaining references**

  ```bash
  grep -n "_POWER_INIT_MTIME\|_init_power_override" \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py
  ```

  Expected: no output.

- [ ] **Step 6: Verify the file is syntactically valid Python**

  ```bash
  python3 -m py_compile \
    platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py \
    && echo "syntax ok"
  ```

  Expected: `syntax ok`

- [ ] **Step 7: Commit**

  ```bash
  git add platform/broadcom/sonic-platform-modules-accton/wedge100s-32x/sonic_platform/sfp.py
  git commit -m "platform: wedge100s: remove _init_power_override; sff_mgr handles PC5-8 power class"
  ```

---

## Task 3: Build the platform .deb

- [ ] **Step 1: Build**

  ```bash
  BLDENV=trixie make target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb
  ```

  Expected: exits 0, `.deb` present in `target/debs/trixie/`.

- [ ] **Step 2: Confirm package contains both changed files**

  ```bash
  dpkg-deb -c target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb \
    | grep -E "pmon_daemon_control|sfp\.py"
  ```

  Expected: both files listed.

---

## Task 4: Deploy and verify on hardware

**Target:** `admin@192.168.88.12`

- [ ] **Step 1: Check target reachability**

  ```bash
  ping -c1 -W2 192.168.88.12 && ssh -o ConnectTimeout=5 admin@192.168.88.12 echo ok
  ```

  If ping succeeds but SSH fails, stop and notify the user to restore key access before continuing.

- [ ] **Step 2: Copy and install the .deb**

  ```bash
  scp target/debs/trixie/sonic-platform-accton-wedge100s-32x_1.1_amd64.deb admin@192.168.88.12:~
  ssh admin@192.168.88.12 "sudo systemctl stop pmon && sudo dpkg -i sonic-platform-accton-wedge100s-32x_1.1_amd64.deb && sudo systemctl start pmon"
  ```

  Expected: `dpkg` exits 0, `pmon` starts without error.

- [ ] **Step 3: Confirm sff_mgr is running**

  ```bash
  ssh admin@192.168.88.12 "sudo docker exec pmon ps aux | grep sff_mgr"
  ```

  Expected: a `sff_mgr` process appears.

- [ ] **Step 4: Identify the PC6 port**

  On the target, find the port whose transceiver shows `ext_id=0xce` (Power Class 6):

  ```bash
  ssh admin@192.168.88.12 "python3 -c \"
  import json, os, time, glob
  for f in sorted(glob.glob('/run/wedge100s/sfp_*_eeprom')):
      port = int(f.split('_')[2])
      try:
          raw = open(f, 'rb').read(256)
          if len(raw) >= 130 and raw[129] == 0xce:
              print(f'port {port}: ext_id=0xce (PC6)')
      except: pass
  \""
  ```

  Note the port number for subsequent steps (call it `N`).

- [ ] **Step 5: Verify oper status is up on the PC6 port**

  ```bash
  ssh admin@192.168.88.12 "show interfaces status | grep EthernetN"
  ```

  Expected: `U` in the oper column.

- [ ] **Step 6: Verify byte 93 = 0x05 (bit 2 set by sff_mgr)**

  Replace `N` with the 0-based port index found in Step 4:

  ```bash
  ssh admin@192.168.88.12 "python3 -c \"
  import json, os, time
  p = N
  req = f'/run/wedge100s/sfp_{p}_read_req'
  rsp = f'/run/wedge100s/sfp_{p}_read_resp'
  try: os.unlink(rsp)
  except: pass
  open(req, 'w').write(json.dumps({'offset': 93, 'length': 1}))
  deadline = time.monotonic() + 5
  while time.monotonic() < deadline:
      if os.path.exists(rsp): break
      time.sleep(0.05)
  result = open(rsp).read().strip()
  print(hex(bytes.fromhex(result)[0]))
  \""
  ```

  Expected: `0x5` (bit 2 = High Power Class Enable set; bit 0 = Power Set from sff_mgr's set_lpmode).

- [ ] **Step 7: Verify TX channels enabled (byte 86 = 0x00)**

  Same IPC pattern as Step 6, offset 86:

  ```bash
  ssh admin@192.168.88.12 "python3 -c \"
  import json, os, time
  p = N
  req = f'/run/wedge100s/sfp_{p}_read_req'
  rsp = f'/run/wedge100s/sfp_{p}_read_resp'
  try: os.unlink(rsp)
  except: pass
  open(req, 'w').write(json.dumps({'offset': 86, 'length': 1}))
  deadline = time.monotonic() + 5
  while time.monotonic() < deadline:
      if os.path.exists(rsp): break
      time.sleep(0.05)
  result = open(rsp).read().strip()
  print(hex(bytes.fromhex(result)[0]))
  \""
  ```

  Expected: `0x0` (all TX channels enabled).

- [ ] **Step 8: Verify DOM data is populated**

  ```bash
  ssh admin@192.168.88.12 "redis-cli -n 6 hgetall 'TRANSCEIVER_DOM_INFO|EthernetN'"
  ```

  Expected: non-zero `rx_power` and `tx_power` values.

- [ ] **Step 9: Regression — existing PC4 port remains up**

  Find a PC1 or PC4 port (ext_id=0xcc, i.e. byte 129=0xcc, bits 7:6=0b11, bits 1:0=0b00).
  Confirm it is still `U`:

  ```bash
  ssh admin@192.168.88.12 "show interfaces status | grep EthernetM"
  ```

  Expected: `U`.

- [ ] **Step 10: Cold-boot regression (optional but recommended)**

  Reboot the switch and after boot verify the PC6 port comes up without re-insertion:

  ```bash
  ssh admin@192.168.88.12 "sudo reboot"
  # wait ~3 minutes for SONiC to boot
  ssh admin@192.168.88.12 "show interfaces status | grep EthernetN"
  ```

  Expected: `U` (sff_mgr replays TRANSCEIVER_INFO on xcvrd startup).
