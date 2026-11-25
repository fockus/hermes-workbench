---
name: smart-home-local-operations
description: "Use when discovering, identifying, or controlling local smart-home and IoT devices such as Hue lights, Matter/miIO/Tuya devices, mDNS/SSDP services, and LAN device inventories."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [smart-home, iot, hue, lan, mdns, ssdp, matter]
    related_skills: []
---

# Smart Home Local Operations

## Overview

This umbrella covers safe local smart-home discovery and control. It combines LAN/IoT reconnaissance with device-specific control such as Philips Hue/OpenHue.

## When to Use

- Discover smart-home or IoT devices on the local network.
- Identify devices through ARP, ping, mDNS/DNS-SD, SSDP/UPnP, miIO, Matter, vendor OUIs, or other safe probes.
- Control Philips Hue lights, rooms, scenes, and bridges.
- Investigate local-network automation failures.

## Workflow

1. Identify local interfaces, subnets, and network constraints.
2. Use passive/light active discovery before protocol-specific probing.
3. Classify devices by hostname, MAC/OUI, advertised services, and protocol responses.
4. For control operations, verify bridge/device target and current state before changing it.
5. Read back final state.

## Common Pitfalls

1. Avoid aggressive scanning on user networks.
2. Device identity is often ambiguous; combine multiple signals.
3. Smart-home changes are physical side effects; verify room/light target first.

## Verification Checklist

- [ ] Network scope and probe intensity were appropriate.
- [ ] Device/room target was verified before control.
- [ ] Final device state was read back.
