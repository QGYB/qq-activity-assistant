# Independent Windows startup adapter

`qq_activity_boot.c` and `qq_activity/native_boot.py` are independently written
for this application. They contain no NapCat, Lagrange, LiteLoader, MinHook or
other QQ project source code or runtime dependencies. Build with Windows x64
Zig C (or a compatible Windows C compiler); the DLL uses Windows system APIs.
The Zig Windows build includes generic MinGW-w64 CRT support; its distribution
notice is retained in `MinGW-w64-LICENSE.txt`. No QQ reference binary is shipped.

Research reference: [NapNeko/NapCat-Windows-Boot](https://github.com/NapNeko/NapCat-Windows-Boot/tree/430722c2cfbe805c4e5d608744494e8eab52c793),
branch `new-napcat-main`, commit `430722c2cfbe805c4e5d608744494e8eab52c793`.
The inspected repository has no LICENSE file, so no source snippets were copied
or vendored. Its file-redirection and process-memory compatibility strategy was
studied. The implementation here uses exact absolute paths, a fixed binary
fingerprint, bounded PE import-table traversal, and transactional Python startup.
The fixed instruction bytes, image metadata and SHA-256 values were measured
directly from the user's installed QQ 9.9.35-52892 executable and QQNT.dll.
Those interoperability facts identify the specific compatibility branch, rather
than importing the reference's wildcard scanner or broad path matching.

The only changed QQ instruction is one conditional-branch byte in process
memory at QQNT.dll RVA `0x514d1a`. Its 32-byte surrounding fingerprint, PE
timestamp `0x69c117ce`, image size `0xd093000` and installed-file SHA-256 must
match. Unsupported builds fail closed. Original QQ files are never written.
Only the exact original package path and exact virtual bootstrap path are
redirected to caller-supplied local files. Normal QQ GUI entry remains the
JavaScript bootstrap's responsibility; the launcher neither hides QQ nor binds
its lifetime to the assistant process.

Build from PowerShell:

```powershell
./native/build.ps1 -Zig C:/path/to/zig.exe
py -m unittest discover -s tests -p test_native_boot.py
```

`launch_qq(qq_root, app_dir, shadow_package, bootstrap, dll_path=None,
extra_args=None)` validates the installed build, creates a new suspended QQ
process, loads this DLL, resumes it, and closes all local handles. Failures
terminate only that newly created child. It never restarts/terminates an
existing QQ session; explicit restart remains with the application caller.

Source compilation and fixture verification are distinct from real QQ runtime
verification. `smoke_host.c` is a disposable native test host for actual DLL
loading and file-redirection tests without starting QQ.

Verified on 2026-10-28: Zig 0.17.0 built the x64 DLL with `-Wall -Wextra -Werror`;
five Python tests passed; an actual suspended `smoke_host.exe` process loaded
the DLL, read redirected virtual-file content, wrote `PASS`, and survived the
launcher's detach. Windows loader initialization runs on a short-lived ntdll
thread while the application's main thread remains suspended. Its mapped-file
identity and instruction bytes are checked before that thread is started.
The installed QQ executable and QQNT.dll passed read-only SHA-256 validation.
PE inspection found AMD64 machine type and only Windows/Universal CRT imports.
This fixture test did not start, stop, attach to, or inject any QQ process.
