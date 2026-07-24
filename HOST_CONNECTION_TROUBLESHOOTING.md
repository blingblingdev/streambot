# macOS Host Connection and Local Network Troubleshooting

## Scope

This document describes how the persistent streambot worker connects to its
paired Sunshine host on macOS 15, how a verified successful connection was
performed, how to distinguish mDNS failure from Local Network Privacy or
interface-routing problems, and how to collect evidence that survives a future
failure.

Never record a host address, pairing identifier, certificate, private key, or
other credential in this document, logs, issue reports, or command output. Use
`<SUNSHINE_IP>` as a placeholder and keep the paired identity under
`.state/<worker-name>/` private.

## Verified successful connection

The verified connection used the project-local Homebrew Python virtual
environment and the existing paired identity. It did not grant a new privacy
permission, re-sign Python, select another interpreter, change the network
interface configuration, pass `--host`, or set `MOONLIGHT_HOST`.

From the repository root, the exact unbounded worker command was:

```bash
.venv/bin/python apps/core-worker/core_worker.py \
  --state-dir .state/poc
```

The connection was verified through the persistent IPC service (`status`
command over the worker's control socket).

A separate bounded reproduction used:

```bash
.venv/bin/python apps/core-worker/core_worker.py \
  --state-dir .state/poc \
  --max-runtime-seconds 12
```

The bounded run reached `observing`, kept automation disabled, received fresh
video frames, reported no reconnect or error, sent no gameplay action, and
stopped normally at the deadline. Five independent discovery-only attempts
from the same virtual-environment interpreter each found exactly one Sunshine
service.

These results prove that this interpreter and paired identity can complete the
mDNS-to-GameStream path in at least one launch context. They do not prove that
macOS will attribute every future launch context to the same responsible code.

## Connection path

Without `--host` or `MOONLIGHT_HOST`, the target worker performs this sequence:

```text
paired identity under .state/poc
        |
        v
mDNS browse for the Sunshine service
        |
        v
require exactly one discovered host
        |
        v
Sunshine HTTP application-list check
        |
        v
GameStream connection and latest-frame observation
        |
        v
private Unix-domain IPC control socket
```

The `RuntimeError` message `Expected exactly one visible Sunshine host` means
that the discovery result contained zero or multiple services. It does not, by
itself, identify why the result was empty.

The current code also supports direct-host connection through `--host` or
`MOONLIGHT_HOST`. That path skips mDNS only. It cannot bypass macOS Local
Network Privacy, a process-specific Network Extension policy, a broken route,
or a failed direct TCP connection. Do not use it as the normal production
path, and never place a host address in repository files or logs.

## Point-in-time diagnostic findings

The affected Mac had both Ethernet and Wi-Fi configured on the same IPv4
subnet. At the time of the successful reproduction:

- The kernel route to the target selected Ethernet.
- The Homebrew virtual-environment Python connected by numeric IPv4 TCP.
- The Apple system Python connected by the same numeric IPv4 TCP test.
- The Homebrew Python completed five consecutive mDNS discoveries, each with
  one result.
- The target worker established GameStream and received frames.

The Homebrew interpreter was an arm64, ad-hoc-signed executable with no Team
Identifier. Its executable signing identifier was not the `org.python.python`
bundle identifier shown by a python.org `Python.app`. Its designated
requirement was based on its code directory hash, and it had a Mach-O UUID.

This distinction matters: the Local Network entry named `Python` in System
Settings does not prove that a different Homebrew executable, or the app that
macOS selects as its responsible code, has the same Local Network privilege.

No failure-time `nehelper` evidence for this specific Python process was
retained. Consequently, the exact responsible-code identity used during the
failed attempt is not yet proven.

## Root-cause assessment

### What the evidence rules out

#### Sunshine availability alone

Sunshine was reachable from Apple-signed system tools at the time of the
reported failure, and the same paired worker later completed a connection.
Sunshine being offline does not explain the process-specific result.

#### mDNS as the initial failure

During the reported failure, numeric-address TCP from the Homebrew Python also
failed with `EHOSTUNREACH`. Numeric-address TCP does not depend on mDNS.
Therefore an empty discovery result can be a downstream symptom of a lower
network-access denial rather than a Bonjour-only defect.

#### Dual-interface routing by itself

Processes that do not bind a source address normally use the same kernel route
for the same destination. Two active interfaces on one subnet can cause ARP,
neighbor-cache, multicast, and service-discovery ambiguity, but this alone does
not naturally explain why one executable is denied while another executable
succeeds at the same time. Interface binding tests are still required to rule
out a combined routing and policy problem.

### Most likely mechanism

The strongest current explanation is macOS Local Network Privacy, or a
process-specific Network Extension policy at the same layer, acting on the
responsible-code identity selected for that launch context.

Local Network Privacy covers direct local-subnet connections and Bonjour
browsing. macOS identifies responsible code, records privilege per user, and
uses code-signing identity plus main-executable UUID as part of its tracking.
Ad-hoc or changing signatures can make that identity unstable. The apparent
application in System Settings may be the launcher or responsible application,
not the child executable that called `socket`.

This mechanism fits the reported evidence:

1. The host and network worked for system Python, `curl`, and `nc`.
2. The Homebrew Python alone received `EHOSTUNREACH` for direct local TCP.
3. The same local-access failure also prevented its mDNS browse from returning
   a service.
4. The same Homebrew executable later succeeded without a route, signature, or
   interpreter change when launched from a different execution context.

The fourth observation is important. It argues against a permanent global
denial attached only to the Python file. It is consistent with a change in
responsible-code attribution, an authorization/cache transition, or an
intermittent macOS Local Network Privacy defect.

### What is not yet proven

It is not yet possible to select one of these sub-causes conclusively:

- A Homebrew rebuild changed the Python Mach-O UUID or code-directory hash.
- macOS attributed different launches to Codex, iTerm, Terminal, Python, or a
  launch agent.
- A Local Network privilege or UUID cache became stale and later refreshed.
- A third-party process-specific Network Extension filter made the decision.
- A dual-interface event coincided with a process-attribution difference.

Capture `nehelper` evidence while the failure is active before declaring one
of these narrower causes verified.

## Why `tccutil reset LocalNetwork` fails

Apple does not provide a supported macOS operation to reset one program's Local
Network privilege to the undetermined state. Local Network state is not exposed
as an ordinary `tccutil` service in the way many other privacy classes are.
Use a new macOS user account or a clean VM snapshot for a clean authorization
test.

## Reproducible verification procedure

### 1. Freeze the interpreter identity

Run these commands during both a successful and failed state, then compare the
results privately:

```bash
python_bin="$(realpath .venv/bin/python)"

codesign -dvvv "$python_bin" 2>&1 | \
  grep -E 'Identifier=|TeamIdentifier=|Signature=|CodeDirectory'
codesign -dr - "$python_bin" 2>&1
dwarfdump --uuid "$python_bin"
shasum -a 256 "$python_bin"
```

If the UUID, designated requirement, or executable digest changed between the
last known-good run and the failure, a Homebrew rebuild changed the code
identity macOS may be tracking.

### 2. Compare interpreters under the same launcher

Use one minimal socket probe with both interpreters:

```bash
.venv/bin/python probe.py <SUNSHINE_IP> 47989
/usr/bin/python3 probe.py <SUNSHINE_IP> 47989
```

Repeat the same pair from each relevant launch context:

1. Apple Terminal
2. iTerm
3. Codex
4. The intended launch agent

Record only interpreter path, parent-process chain, errno, time, selected
interface, executable UUID, and code-signing identity. Do not record the host
address in persistent logs.

If the same Homebrew executable succeeds from one launcher and fails from
another, investigate responsible-code attribution before changing mDNS or
route logic.

### 3. Capture macOS network-policy evidence during the failure

Start this before reproducing the failed connection:

```bash
/usr/bin/log stream --info --debug \
  --predicate 'process == "nehelper" OR process == "nesessionmanager"'
```

Look for messages containing:

- local network allowed or denied;
- responsible process or bundle identifier;
- Team Identifier;
- UUID cache hit, miss, or reset;
- failure to obtain executable UUIDs;
- path matching an existing rule;
- a received or suppressed privacy prompt.

Redact addresses, user paths, and unrelated application identifiers before
sharing the output.

### 4. Separate direct TCP from discovery

Test in this order:

1. Numeric-address TCP from the Homebrew interpreter.
2. Target-library discovery with a five-second timeout.
3. System `dns-sd` browsing for the Sunshine service type.

Interpret the results as follows:

| Direct TCP | Homebrew discovery | Primary investigation |
| --- | --- | --- |
| Fails | Zero | Local Network Privacy, process filtering, or route |
| Works | Zero | Bonjour, multicast, or interface enumeration |
| Works | One | Discovery path is healthy |
| System works, Homebrew fails | Any | Responsible code or process policy |

### 5. Separate Ethernet from Wi-Fi without changing configuration

First inspect the selected route:

```bash
target_host="<SUNSHINE_IP>"
/sbin/route -n get "$target_host" | \
  grep -E 'interface:|gateway:|flags:'
```

Then modify the minimal probe to bind separately to each local source address
before connecting. Do not assume that `en0` is always Wi-Fi or that `en1` is
always Ethernet; obtain the configured interfaces and their functional types
from the current system.

Interpretation:

- Both interpreters fail only through one source interface: investigate that
  interface, ARP, route, switch, or access-point path.
- Only Homebrew Python fails through both source interfaces: investigate Local
  Network Privacy or process filtering.
- Homebrew Python succeeds through one source interface: investigate route and
  interface-specific policy before changing discovery.

### 6. Test a clean per-user authorization state

Use a new macOS user or a VM snapshot created before the executable was first
run. Test Apple Terminal first, then the intended launcher or launch agent.
Observe the privacy prompt, System Settings entry, and `nehelper` output.

This is the supported clean-state test on macOS. Do not rely on
`tccutil reset LocalNetwork`.

### 7. Optional system-level confirmation

macOS 15.5 and later supports the system preferences
`AllowedEthernetLocalNetworkAddresses` and `AllowedWiFiLocalNetworkAddresses`
in the `com.apple.network.local-network` domain. An administrator can designate
a CIDR as exempt from Local Network Privacy, followed by a restart.

If a carefully backed-up, temporary exemption makes the Homebrew probe work,
that is strong evidence that Local Network Privacy was the blocking layer. This
changes system-wide security policy for every program and must not be used
without an exact backup and rollback procedure. Do not use a broad CIDR merely
to avoid fixing responsible-code identity.

## Durable worker deployment

A normal user LaunchAgent provides process persistence but does not bypass
Local Network Privacy. For a durable user-level worker:

- Wrap the launcher in an app-like bundle with a stable bundle identifier.
- Sign it with a stable Apple-issued signing identity.
- Ensure its main executable has a unique, stable Mach-O UUID.
- Declare `AssociatedBundleIdentifiers` in the LaunchAgent property list so
  macOS can identify the responsible bundle.
- Keep the control plane paused until an explicit `resume-automation` command.
- Keep the paired identity and logs in owner-only state directories.
- Verify the Local Network prompt and authorization interactively before
  relying on unattended launch.

A root LaunchDaemon is automatically allowed local-network access according to
Apple's documented macOS behavior, but running this gameplay worker as root is
an unnecessary privilege expansion and is not the recommended solution.

Do not modify or re-sign Homebrew's Python binary in place. A Homebrew upgrade
would replace it, and changing a shared interpreter can affect unrelated virtual
environments. Stabilize the responsible launcher instead.

## Operational decision tree

```text
IPC service responds?
  yes -> reuse the existing worker
  no  -> confirm no worker is starting or stopping
          |
          v
       direct TCP from the intended Python works?
         no  -> compare system Python and capture nehelper policy evidence
         yes -> run target-library mDNS discovery
                  |
                  +-- zero -> investigate Bonjour and both active interfaces
                  +-- one  -> start exactly one persistent worker, paused
                  +-- many -> fail closed and disambiguate the advertised host
```

## Primary references

- [Apple TN3179: Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)
- [Apple TN3127: Inside Code Signing: Requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)
- [Apple: Creating distribution-signed code for macOS](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/)
- [Apple Support: Control access to your local network on Mac](https://support.apple.com/guide/mac-help/control-access-to-your-local-network-on-mac-mchla4f49138/mac)
- [Apple Developer Forums: Local Network Access Permission](https://developer.apple.com/forums/thread/759955)

