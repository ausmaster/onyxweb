//! `chrome_executable()` points here instead of the real Chrome binary, so Chrome's
//! whole process tree dies when onyxweb's own process dies abruptly. chromiumoxide
//! owns the spawn and exposes no `pre_exec` hook, so this indirection is the only lever.
//!
//! Real path comes via `ONYXWEB_REAL_CHROME_PATH`; all other argv/env pass through.
//! Linux execs the real binary in place; macOS/Windows supervise it as a child, since
//! neither has a passive kernel flag like Linux's `PR_SET_PDEATHSIG` to exec into.

use std::env;

const REAL_CHROME_ENV: &str = "ONYXWEB_REAL_CHROME_PATH";

fn real_chrome_path() -> String {
    env::var(REAL_CHROME_ENV).unwrap_or_else(|_| {
        eprintln!("onyxweb_wrapper: {REAL_CHROME_ENV} not set");
        std::process::exit(1);
    })
}

#[cfg(target_os = "linux")]
fn main() {
    use std::os::unix::process::CommandExt;
    let chrome = real_chrome_path();
    // Kernel-tracked flag on this task, not a userspace construct — survives the exec
    // below (PR_SET_PDEATHSIG is cleared only by a setuid/setgid target, which chrome
    // never is here since onyxweb always launches it --no-sandbox).
    ur_taking_me_with_you::die_with_parent();
    let err = std::process::Command::new(&chrome)
        .args(env::args_os().skip(1))
        .exec();
    eprintln!("onyxweb_wrapper: exec {chrome} failed: {err}");
    std::process::exit(1);
}

#[cfg(target_os = "macos")]
fn main() {
    macos::run(real_chrome_path());
}

#[cfg(target_os = "windows")]
fn main() {
    windows::run(real_chrome_path());
}

/// Copy `from`'s bytes to our own stderr — chromiumoxide reads OUR stderr (piped by
/// its own spawn call) for the `DevTools listening on…` line, never Chrome's directly.
#[cfg(any(target_os = "macos", target_os = "windows"))]
fn proxy_stderr(mut from: std::process::ChildStderr) {
    use std::io::{Read, Write};
    std::thread::spawn(move || {
        let mut buf = [0u8; 4096];
        loop {
            match from.read(&mut buf) {
                Ok(0) | Err(_) => return,
                Ok(n) => {
                    if std::io::stderr().write_all(&buf[..n]).is_err() {
                        return;
                    }
                }
            }
        }
    });
}

#[cfg(target_os = "macos")]
mod macos {
    use super::proxy_stderr;
    use std::process::{Command, Stdio};
    use std::time::Duration;

    /// `kill(pid, 0)` sends no signal, only probes existence. `ESRCH` alone means
    /// gone; any other errno (e.g. `EPERM`) still means alive.
    fn alive(pid: libc::pid_t) -> bool {
        if unsafe { libc::kill(pid, 0) } == 0 {
            return true;
        }
        std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
    }

    pub fn run(chrome_path: String) -> ! {
        let parent = unsafe { libc::getppid() };
        let mut child = Command::new(&chrome_path)
            .args(std::env::args_os().skip(1))
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap_or_else(|e| {
                eprintln!("onyxweb_wrapper: spawn {chrome_path} failed: {e}");
                std::process::exit(1);
            });
        let chrome_pid = child.id() as libc::pid_t;
        proxy_stderr(child.stderr.take().expect("piped"));

        // Polled to dodge kqueue's permission quirks on a non-child PID. Watches
        // two ends: a graceful Client.close() talks to Chrome directly, not us.
        loop {
            if !alive(parent) {
                unsafe {
                    libc::kill(chrome_pid, libc::SIGKILL);
                }
                std::process::exit(1);
            }
            match child.try_wait() {
                Ok(Some(status)) => std::process::exit(status.code().unwrap_or(1)),
                Ok(None) => std::thread::sleep(Duration::from_millis(200)),
                Err(_) => std::process::exit(1),
            }
        }
    }
}

#[cfg(target_os = "windows")]
mod windows {
    use super::proxy_stderr;
    use std::process::{Command, Stdio};
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcessId, INFINITE, OpenProcess, PROCESS_SYNCHRONIZE, PROCESS_TERMINATE,
        TerminateProcess, WaitForSingleObject,
    };

    /// Windows has no `getppid()`; walking a process snapshot for our own entry's
    /// `th32ParentProcessID` is the documented way to find it.
    fn find_parent_pid() -> Option<u32> {
        unsafe {
            let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if snap == INVALID_HANDLE_VALUE {
                return None;
            }
            let me = GetCurrentProcessId();
            let mut entry: PROCESSENTRY32W = std::mem::zeroed();
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            let mut found = None;
            if Process32FirstW(snap, &mut entry) != 0 {
                loop {
                    if entry.th32ProcessID == me {
                        found = Some(entry.th32ParentProcessID);
                        break;
                    }
                    if Process32NextW(snap, &mut entry) == 0 {
                        break;
                    }
                }
            }
            CloseHandle(snap);
            found
        }
    }

    pub fn run(chrome_path: String) -> ! {
        let parent_handle: HANDLE = find_parent_pid()
            .map(|pid| unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) })
            .unwrap_or(std::ptr::null_mut());
        if parent_handle.is_null() {
            eprintln!(
                "onyxweb_wrapper: could not resolve/open parent process; running unprotected"
            );
        }
        // HANDLE (*mut c_void) isn't Send; carry it across the thread boundary as the
        // plain address it is and rebuild the pointer inside the closure.
        let parent_addr = parent_handle as usize;

        let mut cmd = Command::new(&chrome_path);
        cmd.args(std::env::args_os().skip(1))
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        // Assigns Chrome to a kill-on-close job tied to THIS process's own handle
        // table, so Chrome dies the instant we exit — for any reason, crash included.
        let mut child = ur_taking_me_with_you::spawn_dying_with_parent(cmd).unwrap_or_else(|e| {
            eprintln!("onyxweb_wrapper: spawn {chrome_path} failed: {e}");
            std::process::exit(1);
        });
        proxy_stderr(child.stderr.take().expect("piped"));
        let chrome_pid = child.id();

        if !parent_handle.is_null() {
            std::thread::spawn(move || unsafe {
                WaitForSingleObject(parent_addr as HANDLE, INFINITE);
                // Direct kill too — an inherited handle in Chrome's own tree can
                // keep the job's last reference open, so don't rely on that alone.
                let h = OpenProcess(PROCESS_TERMINATE, 0, chrome_pid);
                if !h.is_null() {
                    TerminateProcess(h, 1);
                }
                std::process::exit(1);
            });
        }
        let status = child.wait().unwrap_or_else(|_| std::process::exit(1));
        std::process::exit(status.code().unwrap_or(1));
    }
}
