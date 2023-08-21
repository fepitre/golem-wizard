#!/usr/bin/python3
import argparse
import locale
import logging
import os
import subprocess
import sys

from dialog import Dialog
from textwrap import wrap

locale.setlocale(locale.LC_ALL, "")


PCI_VGA_CLASS_ID = "0300"
PCI_AUDIO_CLASS_ID = "0403"
PCI_BRIDGE_CLASS_ID = "0604"


logger = logging.getLogger(__name__)


class WizardError(Exception):
    pass


def get_iommu_groups():
    iommu_groups = []
    if os.path.exists("/sys/kernel/iommu_groups"):
        iommu_groups = os.listdir("/sys/kernel/iommu_groups")
    return sorted(iommu_groups, key=lambda x: int(x))


def get_iommu_group_devices(iommu_group):
    devices = []
    devices_path = f"/sys/kernel/iommu_groups/{iommu_group}/devices"
    if os.path.exists(devices_path):
        devices = os.listdir(devices_path)
    return devices


def get_pci_full_string_description_from_slot(slot):
    result = subprocess.run(["lspci", "-s", slot], capture_output=True, text=True)
    return result.stdout.strip()


def get_pci_short_string_description_from_slot(slot):
    full_description = get_pci_full_string_description_from_slot(slot)
    return full_description.split(": ", 1)[1]


def list_pci_devices_in_iommu_group(devices):
    return [get_pci_full_string_description_from_slot(device) for device in devices]


def get_pid_vid_from_slot(slot):
    result = subprocess.run(["lspci", "-n", "-s", slot], capture_output=True, text=True)
    return result.stdout.split()[2]


def get_class_from_slot(slot):
    result = subprocess.run(["lspci", "-n", "-s", slot], capture_output=True, text=True)
    return result.stdout.split()[1].rstrip(":")


def parse_devices(devices, allowed_classes):
    parsed_devices = {}
    for device in devices:
        device_class = get_class_from_slot(device)
        if device_class in allowed_classes:
            parsed_devices.setdefault(device_class, []).append(device)
    return parsed_devices


def has_only_allowed_devices(parsed_devices, devices):
    filtered_devices_list = [
        device for devices in parsed_devices.values() for device in devices
    ]
    return set(filtered_devices_list) == set(devices)


def is_pci_bridge_of_device(pci_bridge_device: str, device: str):
    parsed_bridge_device = pci_bridge_device.split(":")
    if len(parsed_bridge_device) != 3:
        raise WizardError(f"Cannot parse PCI bridge device: '{pci_bridge_device}'")
    domain, bus, _ = parsed_bridge_device
    device_path = f"/sys/bus/pci/devices/{device}"
    real_device_path = f"/sys/devices/pci{domain}:{bus}/{pci_bridge_device}/{device}"
    return os.path.realpath(device_path) == real_device_path


def is_pci_supplier_of_device(pci_supplier_device: str, device: str):
    device_path = f"/sys/bus/pci/devices/{device}/supplier:pci:{pci_supplier_device}"
    return os.path.exists(device_path)


def select_gpu_compatible(allow_pci_bridge=True):
    allowed_classes = [PCI_VGA_CLASS_ID, PCI_AUDIO_CLASS_ID]
    if allow_pci_bridge:
        allowed_classes.append(PCI_BRIDGE_CLASS_ID)

    gpu_list = []
    bad_isolation_groups = {}

    iommu_groups = get_iommu_groups()
    for iommu_group in iommu_groups:
        devices = get_iommu_group_devices(iommu_group)
        parsed_devices = parse_devices(devices, allowed_classes)

        # Check if a GPU exists
        if PCI_VGA_CLASS_ID not in parsed_devices:
            continue

        pci_vga_device = parsed_devices[PCI_VGA_CLASS_ID][0]
        pci_bridge_device = parsed_devices.get(PCI_BRIDGE_CLASS_ID, [""])[0]
        pci_audio_device = parsed_devices.get(PCI_AUDIO_CLASS_ID, [""])[0]

        # Check if we have:
        # 1. Only allowed devices
        # 2. At most one PCI bridge device
        # 3. At most one PCI audio device
        # 4. Only one GPU (we checked that one exists before)
        # 5. PCI bridge device being parent of GPU device
        # 6. GPU device is a supplier for audio device
        if (
            not has_only_allowed_devices(parsed_devices, devices)
            or len(parsed_devices.get(PCI_BRIDGE_CLASS_ID, [])) > 1
            or len(parsed_devices.get(PCI_AUDIO_CLASS_ID, [])) > 1
            or len(parsed_devices[PCI_VGA_CLASS_ID]) > 1
            or (
                pci_bridge_device
                and not is_pci_bridge_of_device(pci_bridge_device, pci_vga_device)
            )
            or (
                pci_audio_device
                and not is_pci_supplier_of_device(pci_vga_device, pci_audio_device)
            )
        ):
            bad_isolation_groups[iommu_group] = list_pci_devices_in_iommu_group(devices)
            continue

        gpu_vga_slot = parsed_devices[PCI_VGA_CLASS_ID][0]
        vfio_devices = (
            parsed_devices[PCI_VGA_CLASS_ID] + parsed_devices[PCI_AUDIO_CLASS_ID]
        )
        vfio = ",".join(get_pid_vid_from_slot(device) for device in vfio_devices)

        gpu_list.append(
            {
                "description": get_pci_full_string_description_from_slot(gpu_vga_slot),
                "vfio": vfio,
                "slot": gpu_vga_slot,
            }
        )

    return gpu_list, bad_isolation_groups


class WizardDialog:
    dialog = Dialog(dialog="dialog", pass_args_via_file=False)

    @classmethod
    def __init__(cls):
        cls.dialog.set_background_title("GOLEM Provider Wizard")

    @classmethod
    def _auto_height(cls, width, text):
        _max = max(8, 5 + len(wrap(text, width=width)))  # Min of 8 rows
        _min = min(22, _max)  # Max of 22 rows
        return _min

    @classmethod
    def yesno(cls, text, **info):
        default = {"colors": True, "width": 72, "height": 8}
        default.update(info)

        code = cls.dialog.yesno(text, **default)

        if code == cls.dialog.OK:
            return True
        elif code == cls.dialog.CANCEL:
            return False
        elif code == cls.dialog.ESC:
            sys.exit("Escape key pressed. Exiting.")

    @classmethod
    def msgbox(cls, text, **info):
        default = {"colors": True, "width": 72, "height": 8}
        default.update(info)

        if not default["height"]:
            default["height"] = cls._auto_height(default["width"], default["text"])

        return cls.dialog.msgbox(text, **default)

    @classmethod
    def menu(cls, text, **info):
        default = {"colors": True, "width": 72, "height": 8}
        default.update(info)

        if not default["height"]:
            default["height"] = cls._auto_height(default["width"], default["text"])

        return cls.dialog.menu(text, **default)


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--relax-gpu-isolation",
        action="store_true",
        default=False,
        help="Relax GPU isolation. For example, allow PCI bridge on which the GPU is connected in the same IOMMU group.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    d = WizardDialog()
    if d.yesno("Do you want to select a GPU?"):
        gpu_list, bad_isolation_groups = select_gpu_compatible(
            allow_pci_bridge=args.relax_gpu_isolation
        )
        if not gpu_list:
            if bad_isolation_groups:
                for iommu_group in bad_isolation_groups:
                    devices = bad_isolation_groups.get(iommu_group, [])
                    if devices:
                        msg = f"IOMMU Group '{iommu_group}' has bad isolation:\n\n"
                        for device in devices:
                            msg += "  " + device + "\n"
                        d.msgbox(msg, width=640)

            d.msgbox("No compatible GPU available.")
            return

        gpu_choices = [(gpu["description"], "") for gpu in gpu_list]
        code, gpu_tag = d.menu("Select a GPU:", choices=gpu_choices)

        if code:
            selected_gpu = None
            for gpu in gpu_list:
                if gpu["description"] == gpu_tag:
                    selected_gpu = gpu
                    break

            if selected_gpu:
                d.msgbox(
                    f"Selected GPU: {selected_gpu['slot']} (VFIO: {selected_gpu['vfio']})"
                )
            else:
                d.msgbox("Invalid GPU selection.")
        else:
            d.msgbox("No GPU selected.")
    else:
        d.msgbox("No GPU selection.")


if __name__ == "__main__":
    main()
