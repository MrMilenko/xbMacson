"""Per-console differences: port, byte order, registers, launch, screenshot."""


class ConsoleProfile:
    def __init__(self, key, name, port, endian, arch, pc_reg, sp_reg, fp_reg,
                 exe_ext, channel, screenshot, reboot_for_deploy, aliases=()):
        self.key = key
        self.name = name
        self.port = port
        self.endian = endian          # struct prefix: "<" or ">"
        self.arch = arch              # "x86" or "ppc"
        self.pc_reg = pc_reg          # context key, lower case
        self.sp_reg = sp_reg
        self.fp_reg = fp_reg          # frame pointer / back chain register
        self.exe_ext = exe_ext
        self.channel = channel        # default notification channel
        self.screenshot = screenshot  # "linear" or "xenos"
        self.reboot_for_deploy = reboot_for_deploy
        self.aliases = aliases

    def launch_command(self, path):
        if self.key == "x360":
            directory = path.rsplit("\\", 1)[0] if "\\" in path else path
            return f'magicboot title="{path}" directory="{directory}"'
        # "debug" keeps XBDM loaded in the new title so we keep seeing output.
        return f'magicboot title="{path}" debug'

    def __repr__(self):
        return f"ConsoleProfile({self.key})"


OG_XBOX = ConsoleProfile(
    key="og", name="Xbox", port=731, endian="<", arch="x86",
    pc_reg="eip", sp_reg="esp", fp_reg="ebp", exe_ext=".xbe",
    channel="notifyat", screenshot="linear", reboot_for_deploy="reboot",
    aliases=("xbox", "ogxbox", "xb"),
)

XBOX_360 = ConsoleProfile(
    key="x360", name="Xbox 360", port=730, endian=">", arch="ppc",
    pc_reg="iar", sp_reg="gpr1", fp_reg="gpr1", exe_ext=".xex",
    channel="notify", screenshot="xenos", reboot_for_deploy="magicboot cold",
    aliases=("360", "xenon", "xbox360"),
)

PROFILES = {p.key: p for p in (OG_XBOX, XBOX_360)}


def get_profile(name):
    if isinstance(name, ConsoleProfile):
        return name
    key = (name or "og").lower()
    for p in PROFILES.values():
        if key == p.key or key in p.aliases:
            return p
    raise ValueError(f"unknown console profile {name!r} (use og or x360)")
