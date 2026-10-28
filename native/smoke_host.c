/* SPDX-License-Identifier: AGPL-3.0-only
 * SPDX-FileCopyrightText: 2026 QGYB and contributors
 */
/* Harmless fixture for testing DLL startup and exact-path file redirection.
   Does not open QQ, communicate externally, or touch installed QQ files. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string.h>
int main(void) {
    WCHAR path[32768]; char data[32]={0}; DWORD length=0;
    if (!GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"GetTickCount64")) return 10;
    if (!GetEnvironmentVariableW(L"QQ_ACTIVITY_VIRTUAL_BOOTSTRAP",path,32768)) return 11;
    HANDLE file=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,0,NULL);
    if (file==INVALID_HANDLE_VALUE) return 12;
    BOOL read=ReadFile(file,data,sizeof(data)-1,&length,NULL); CloseHandle(file);
    if (!read || strcmp(data,"own-native-smoke")!=0) return 13;
    if (!GetEnvironmentVariableW(L"QQ_ACTIVITY_SMOKE_RESULT",path,32768)) return 14;
    file=CreateFileW(path,GENERIC_WRITE,0,NULL,CREATE_ALWAYS,0,NULL);
    if (file==INVALID_HANDLE_VALUE) return 15;
    WriteFile(file,"PASS",4,&length,NULL); CloseHandle(file);
    Sleep(1800); return 0;
}
