/* SPDX-License-Identifier: AGPL-3.0-only
 * SPDX-FileCopyrightText: 2026 QGYB and contributors
 */
/* Independently implemented, build-specific QQ GUI bootstrap.
 * No upstream QQ-project source is included. See RESEARCH.md. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>

#define PATH_CAP 32768
static WCHAR original_manifest[PATH_CAP], shadow_manifest[PATH_CAP];
static WCHAR virtual_script[PATH_CAP], real_script[PATH_CAP], expected_module[PATH_CAP];
static FARPROC (WINAPI *system_resolve)(HMODULE,LPCSTR);
static HANDLE (WINAPI *system_open)(LPCWSTR,DWORD,DWORD,LPSECURITY_ATTRIBUTES,DWORD,DWORD,HANDLE);
typedef BOOL (WINAPI *InfoByName)(PCWSTR,int,PVOID,ULONG);
static InfoByName system_info, base_info;
static LONG initialized;

static const WCHAR *without_prefix(const WCHAR *p) {
    return p && wcsncmp(p,L"\\\\?\\",4)==0 ? p+4 : p;
}
static BOOL same_path(const WCHAR *a,const WCHAR *b) {
    a=without_prefix(a); b=without_prefix(b);
    if (!a || !b) return FALSE;
    while (*a && *b) {
        WCHAR x=*a++,y=*b++;
        if (x==L'/') x=L'\\'; if (y==L'/') y=L'\\';
        if (x>=L'A' && x<=L'Z') x+=32;
        if (y>=L'A' && y<=L'Z') y+=32;
        if (x!=y) return FALSE;
    }
    return *a==*b;
}
static PCWSTR mapped_path(PCWSTR path) {
    if (same_path(path,original_manifest)) return shadow_manifest;
    if (same_path(path,virtual_script)) return real_script;
    return path;
}
static HANDLE WINAPI open_redirect(PCWSTR path,DWORD access,DWORD share,
    LPSECURITY_ATTRIBUTES sa,DWORD creation,DWORD flags,HANDLE template_file) {
    /* Only reads may be redirected; never write into a shadow through QQ. */
    if (creation==OPEN_EXISTING && !(access & (GENERIC_WRITE|GENERIC_ALL|DELETE|FILE_WRITE_DATA|FILE_APPEND_DATA|
                                             FILE_WRITE_EA|FILE_WRITE_ATTRIBUTES|WRITE_DAC|WRITE_OWNER)))
        path=mapped_path(path);
    return system_open(path,access,share,sa,creation,flags,template_file);
}
static BOOL WINAPI info_redirect(PCWSTR path,int kind,PVOID result,ULONG size) {
    return system_info(mapped_path(path),kind,result,size);
}
static FARPROC WINAPI resolve_redirect(HMODULE module,LPCSTR name);

static IMAGE_NT_HEADERS64 *headers(HMODULE module) {
    BYTE *base=(BYTE*)module;
    IMAGE_DOS_HEADER *dos=(IMAGE_DOS_HEADER*)base;
    if (!base || dos->e_magic!=IMAGE_DOS_SIGNATURE || dos->e_lfanew<0 || dos->e_lfanew>0x100000) return NULL;
    IMAGE_NT_HEADERS64 *nt=(IMAGE_NT_HEADERS64*)(base+dos->e_lfanew);
    if (nt->Signature!=IMAGE_NT_SIGNATURE || nt->FileHeader.Machine!=IMAGE_FILE_MACHINE_AMD64 ||
        nt->OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR64_MAGIC) return NULL;
    return nt;
}
static BOOL within(DWORD rva,SIZE_T length,DWORD image_size) {
    return rva<image_size && length<=image_size-rva;
}
static BOOL replace_slot(ULONG_PTR *slot,ULONG_PTR value) {
    DWORD before,ignored;
    if (!VirtualProtect(slot,sizeof(*slot),PAGE_READWRITE,&before)) return FALSE;
    InterlockedExchangePointer((PVOID volatile*)slot,(PVOID)value);
    return VirtualProtect(slot,sizeof(*slot),before,&ignored);
}
static BOOL install_imports(HMODULE module,BOOL require_resolver) {
    IMAGE_NT_HEADERS64 *nt=headers(module);
    if (!nt) return FALSE;
    DWORD total=nt->OptionalHeader.SizeOfImage;
    IMAGE_DATA_DIRECTORY table=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!within(table.VirtualAddress,table.Size,total) || table.Size<sizeof(IMAGE_IMPORT_DESCRIPTOR)) return FALSE;
    BYTE *base=(BYTE*)module;
    BOOL saw_resolver=FALSE;
    for (DWORD offset=0;offset+sizeof(IMAGE_IMPORT_DESCRIPTOR)<=table.Size;offset+=sizeof(IMAGE_IMPORT_DESCRIPTOR)) {
        IMAGE_IMPORT_DESCRIPTOR *descriptor=(IMAGE_IMPORT_DESCRIPTOR*)(base+table.VirtualAddress+offset);
        if (!descriptor->Name) break;
        DWORD rva=descriptor->FirstThunk;
        if (!rva) continue;
        for (;within(rva,sizeof(ULONG_PTR),total);rva+=sizeof(ULONG_PTR)) {
            ULONG_PTR *slot=(ULONG_PTR*)(base+rva),replacement=0,current=*slot;
            if (!current) break;
            if (current==(ULONG_PTR)system_resolve) { replacement=(ULONG_PTR)resolve_redirect; saw_resolver=TRUE; }
            else if (current==(ULONG_PTR)system_open) replacement=(ULONG_PTR)open_redirect;
            else if (system_info && (current==(ULONG_PTR)system_info || current==(ULONG_PTR)base_info)) replacement=(ULONG_PTR)info_redirect;
            if (replacement && !replace_slot(slot,replacement)) return FALSE;
        }
    }
    return !require_resolver || saw_resolver;
}
static BOOL prepare_qq_module(HMODULE module) {
    WCHAR actual[PATH_CAP];
    DWORD length=GetModuleFileNameW(module,actual,PATH_CAP);
    if (!length || length>=PATH_CAP || !same_path(actual,expected_module)) return FALSE;
    IMAGE_NT_HEADERS64 *nt=headers(module);
    if (!nt || nt->FileHeader.TimeDateStamp!=0x69c117ce || nt->OptionalHeader.SizeOfImage!=0x0d093000) return FALSE;
    /* Exact bytes read from the locally installed 9.9.35-52892 binary.
       Python also requires its full SHA-256 before creating the process. */
    static const BYTE expected[]={0xe8,0x1e,0xad,0xce,0xff,0xe8,0x39,0xc4,0xff,0xff,0x84,0xc0,0x0f,0x85,0x5f,0x01,
        0x00,0x00,0x48,0x8d,0x0d,0xba,0x0f,0x00,0x00,0xe8,0x99,0x05,0xde,0x04,0xf6,0x44};
    BYTE *site=(BYTE*)module+0x00514d0d;
    if (memcmp(site,expected,sizeof(expected))) return FALSE;
    if (!install_imports(module,FALSE)) return FALSE;
    DWORD before,ignored;
    if (!VirtualProtect(site+13,1,PAGE_EXECUTE_READWRITE,&before)) return FALSE;
    site[13]=0x84;
    BOOL restored=VirtualProtect(site+13,1,before,&ignored);
    BOOL flushed=FlushInstructionCache(GetCurrentProcess(),site+13,1);
    return restored && flushed;
}
static FARPROC WINAPI resolve_redirect(HMODULE module,LPCSTR name) {
    FARPROC result=system_resolve(module,name);
    if (!result || (ULONG_PTR)name<=0xffff) return result;
    if (system_info && ((ULONG_PTR)result==(ULONG_PTR)system_info || (ULONG_PTR)result==(ULONG_PTR)base_info)) return (FARPROC)(void*)info_redirect;
    if ((ULONG_PTR)result==(ULONG_PTR)system_open) return (FARPROC)(void*)open_redirect;
    if (strcmp(name,"ExportedContentMain")==0 || strcmp(name,"QQMain")==0) {
        LONG previous=InterlockedCompareExchange(&initialized,1,0);
        if (previous==0) {
            if (!prepare_qq_module(module)) ExitProcess(0x51410001);
            InterlockedExchange(&initialized,2);
        } else if (previous!=2) ExitProcess(0x51410002);
    }
    return result;
}
static BOOL setting(PCWSTR key,PWSTR destination) {
    DWORD n=GetEnvironmentVariableW(key,destination,PATH_CAP);
    return n>0 && n<PATH_CAP && destination[1]==L':';
}
BOOL WINAPI DllMain(HINSTANCE self,DWORD reason,LPVOID reserved) {
    (void)reserved;
    if (reason!=DLL_PROCESS_ATTACH) return TRUE;
    DisableThreadLibraryCalls(self);
    if (!setting(L"QQ_ACTIVITY_ORIGINAL_PACKAGE",original_manifest) ||
        !setting(L"QQ_ACTIVITY_SHADOW_PACKAGE",shadow_manifest) ||
        !setting(L"QQ_ACTIVITY_VIRTUAL_BOOTSTRAP",virtual_script) ||
        !setting(L"QQ_ACTIVITY_BOOTSTRAP",real_script) ||
        !setting(L"QQ_ACTIVITY_MODULE",expected_module)) return FALSE;
    system_resolve=GetProcAddress; system_open=CreateFileW;
    system_info=(InfoByName)GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"GetFileInformationByName");
    base_info=(InfoByName)GetProcAddress(GetModuleHandleW(L"kernelbase.dll"),"GetFileInformationByName");
    if (!system_info) system_info=base_info;
    return install_imports(GetModuleHandleW(NULL),TRUE);
}
