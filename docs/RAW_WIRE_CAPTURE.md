# Independent MASsoft Wire Capture

Use this commissioning check before changing parsing based on a new view format.
`scripts/capture_massoft_wire.py` uses only Python's standard library and direct
TCP sockets. It imports no IOC, client, protocol, configuration, or timing module.
It publishes no PVs and never contacts the Archiver.

`DataRawLine` is not reconstructed from parsed floats, but the client caches it
only after row parsing succeeds. That PV is therefore not a complete wire trace.
This diagnostic retains malformed, empty, zero, repeated, and non-UTF-8 records.
It only checks the numeric greeting/file acknowledgement and frames CRLF records
to limit the capture. It does not interpret any legend, date, ms, or intensity cell.

## Preparation

- Leave the MASsoft experiment running and both views open. Keep View 2 in its
  Real Time display mode. Record any different view settings when sharing results.
- Stop only the isolated TEST IOC with Ctrl+C if it is still running, to avoid
  unnecessary diagnostic traffic. Do not stop another production IOC or the scan.
  `Acquire=0` alone does not close an already-running IOC's hot-links.
- Supply the exact full Windows path to the already-open `.exp`, not a CSV or
  containing directory. `-f` associates that file but can open a different file if
  given the wrong name. This script never sends Go, Abort, Close, or Export.

## Run From WS5 PowerShell

Unlike a PV-serving IOC, this diagnostic does not need an IOC server for Archiver
discovery. WS5 must be able to reach the MASsoft INST address on TCP 5026.

```powershell
Set-Location C:\repo\Hiden
$experiment = Read-Host 'Full Windows path to the already-running .exp (no surrounding quotes)'
.\.venv\Scripts\python.exe scripts/capture_massoft_wire.py --host 10.66.58.227 --file "$experiment" --rows 5
```

Type normal single backslashes at the prompt; unlike EPICS `caput -S`, no
backslash-escape decoding is performed on this argument. No CSV export is needed.

If WS5 cannot reach the INST network, run the same standalone script with Python
on IOC2 instead. Transfer just the diagnostic without committing/deploying the IOC:

```powershell
scp .\scripts\capture_massoft_wire.py xf08id@xf08idb-ioc2.nsls2.bnl.local:/tmp/hiden-capture-massoft-wire.py
```

Authenticate through the normal approved SSH workflow. In the existing IOC2 Bash
session, run:

```bash
cd /nsls2/auto-storage/iss/shared/config/repos/Hiden
read -r -p 'Full Windows path to the already-running .exp: ' experiment
pixi run --locked python /tmp/hiden-capture-massoft-wire.py --host 10.66.58.227 --file "$experiment" --rows 5
```

## What Is Captured

For each of Views 1 and 2, it opens one legends connection and one data connection,
sequentially. Every connection reads its greeting, sends `-f"<full path>" -d15`,
and waits for acknowledgement before requesting the link:

```text
-lLegends -v1 -d15
-lData -v1 -c1 -t1 -m1 -d15
-lLegends -v2 -d15
-lData -v2 -c1 -t1 -m1 -d15
```

No commands are sent on a socket after its link request. This follows the supplied
HA-085-109 manual, sections 1.2, 2.1.1, and 3.2. A 30-second acknowledgement timeout
exceeds the 15-second server retry window. Each data/legends read has a total
60-second budget; partial evidence survives timeout. Avoid interrupting a pending
association command; let its timeout finish. No command is retried by this script.

The defaults explicitly request both time columns. They do not force the tabular
view to display Real Time, seek the latest sample, or suppress startup history.
For a separate format check, `--time-format 0 --ms-format 1` can reproduce the
ms-only request without changing a running IOC's settings.

Output goes to a unique ignored `analysis/massoft-wire-...` directory:

- `view1-data.rx.bin`, etc.: every original received byte, including greeting,
  acknowledgement, CRLF delimiters, partial records, and any extra records in the
  last `recv()` chunk. TCP chunks are not necessarily MASsoft row boundaries.
- `*.tx.bin`: bytes successfully submitted to the socket, including delimiters.
- `events.jsonl`: readable byte representations of complete records, command
  attempts, receive-chunk offsets/times, completion and errors. A receive time is
  a host observation, not a MASsoft acquisition timestamp.
- `summary.json`: exact file, endpoint, requested formats, and any errors.

`b'...\t...\r\n'` in the transcript is a lossless byte representation, not literal
backslash characters substituted into the binary capture. Capture success does
not mean valid MASsoft data: refusals or unexpected records remain visible.

Share `events.jsonl` and `summary.json`; retain the `.bin` files for byte-level
checks. Compare legends and all values by matching source records, not receive
time or a GUI's latest row. New connections may replay the beginning of the file.
This small sample can validate formatting/mapping but not long-term timing,
latest-row delivery, or production readiness.
