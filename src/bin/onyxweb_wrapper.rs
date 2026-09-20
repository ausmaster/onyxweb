//! `chrome_executable()` points here instead of the real Chrome binary, so Chrome dies
//! with an abruptly-killed onyxweb process — chromiumoxide spawns it with no `pre_exec`
//! hook, so this indirection is the only lever. Real path via `ONYXWEB_REAL_CHROME_PATH`.
//! Linux execs in place (`PR_SET_PDEATHSIG`); macOS/Windows have no such flag, so they
//! supervise Chrome as a child instead.

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
    // Survives the exec below; only a setuid/setgid target clears it, and Chrome's own
    // binary never is one (a sandbox helper would be a separate child).
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

/// Copies `from`'s bytes to our own stderr — chromiumoxide reads OUR piped stderr for
/// the `DevTools listening on…` line, never Chrome's directly.
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
    use std::process::{Child, Command, Stdio};

    /// `kill(pid, 0)` misreports an unreaped zombie as alive on macOS (measured), and
    /// our real parent never reaps chrome — so watch the kernel's own exit event instead.
    fn watch_exit(kq: libc::c_int, pid: libc::pid_t) -> std::io::Result<()> {
        let mut kev: libc::kevent = unsafe { std::mem::zeroed() };
        kev.ident = pid as usize;
        kev.filter = libc::EVFILT_PROC;
        kev.flags = libc::EV_ADD | libc::EV_ENABLE | libc::EV_ONESHOT;
        kev.fflags = libc::NOTE_EXIT;
        let rc = unsafe { libc::kevent(kq, &kev, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
        if rc < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
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

        // Watches both our parent and chrome itself — a graceful Client.close()
        // talks to chrome directly over CDP, never to us.
        let kq = unsafe { libc::kqueue() };
        if kq < 0 || watch_exit(kq, parent).is_err() || watch_exit(kq, chrome_pid).is_err() {
            eprintln!(
                "onyxweb_wrapper: kqueue setup failed ({}); running unprotected",
                std::io::Error::last_os_error()
            );
            exit_with(&mut child);
        }

        let mut events: [libc::kevent; 1] = unsafe { std::mem::zeroed() };
        let n = unsafe {
            libc::kevent(
                kq,
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                1,
                std::ptr::null(),
            )
        };
        if n <= 0 {
            eprintln!(
                "onyxweb_wrapper: kevent wait failed ({}); killing chrome defensively",
                std::io::Error::last_os_error()
            );
            unsafe { libc::kill(chrome_pid, libc::SIGKILL) };
            std::process::exit(1);
        }
        let fired = events[0].ident as libc::pid_t;
        if fired == chrome_pid {
            // Chrome exited on its own; match its exit code rather than kill anything.
            exit_with(&mut child);
        }
        // Our own parent exited — chrome has no other reason to live.
        unsafe { libc::kill(chrome_pid, libc::SIGKILL) };
        std::process::exit(1);
    }

    fn exit_with(child: &mut Child) -> ! {
        match child.wait() {
            Ok(status) => std::process::exit(status.code().unwrap_or(1)),
            Err(_) => std::process::exit(1),
        }
    }
}

#[cfg(target_os = "windows")]
mod windows {
    use super::proxy_stderr;
    use std::os::windows::io::AsRawHandle;
    use std::process::{Command, Stdio};
    use windows_sys::Win32::Foundation::{
        CloseHandle, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcessId, INFINITE, OpenProcess, PROCESS_SYNCHRONIZE, TerminateProcess,
        WaitForMultipleObjects,
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

        let mut cmd = Command::new(&chrome_path);
        cmd.args(std::env::args_os().skip(1))
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        // Assigns chrome to a kill-on-close job on our own handle table — dies the
        // instant we exit, crash included.
        let mut child = ur_taking_me_with_you::spawn_dying_with_parent(cmd).unwrap_or_else(|e| {
            eprintln!("onyxweb_wrapper: spawn {chrome_path} failed: {e}");
            std::process::exit(1);
        });
        proxy_stderr(child.stderr.take().expect("piped"));
        // CreateProcess's own handle already carries full rights (we created it),
        // unlike the parent's, which needs its own OpenProcess to become waitable.
        let chrome_handle = child.as_raw_handle() as HANDLE;

        if parent_handle.is_null() {
            let status = child.wait().unwrap_or_else(|_| std::process::exit(1));
            std::process::exit(status.code().unwrap_or(1));
        }

        // One wait, two ends, mirroring the macOS kqueue design — chrome exits on its
        // own via a graceful Client.close() talking to it directly, never to us.
        let handles = [parent_handle, chrome_handle];
        let rc = unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, INFINITE) };
        if rc == WAIT_OBJECT_0 + 1 {
            // Chrome exited on its own; match its exit code rather than kill anything.
            let status = child.wait().unwrap_or_else(|_| std::process::exit(1));
            std::process::exit(status.code().unwrap_or(1));
        }
        // Parent exited (or the wait failed) — chrome has no reason to live. Direct
        // kill, not just the job: an inherited handle in chrome's own tree can keep
        // the job's last reference open otherwise.
        unsafe { TerminateProcess(chrome_handle, 1) };
        std::process::exit(1);
    }
}
