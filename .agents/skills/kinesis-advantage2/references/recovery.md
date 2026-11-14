# Recovering an unreadable `active/` folder

On 2026-11-14 the directory block of `active/` was found erased (all `FF`,
its FAT entry free) while the files' data survived as orphan chains. Linux
gave I/O errors on `active/`, and the keyboard fell back to factory key
codes after the drive was next closed. Kinesis's own advice for 2MB drives
is Windows scan-and-repair; this is the Linux route that worked. Each
command touching the device needs the user's approval, and the auto-mode
classifier may make the user run it with `!`.

1. **Copy the drive** into the repo's `tmp/` (read only, about 30 s):
   `sudo dd if=/dev/<dev> of=tmp/vdrive.img bs=4k`, after the user has
   checked the label. Keep an untouched duplicate, `tmp/vdrive.orig.img`.
2. **Repair the copy**: `fsck.vfat -a tmp/vdrive.img`. Lost chains become
   `FSCKnnnn.REC` files in the root. Read them with
   `MTOOLS_SKIP_CHECK=1 mtype -i tmp/vdrive.img ::/fsck0000.rec`; the
   current `qwerty.txt`, `state.txt` and `version.txt` are among them,
   alongside stale older versions and old macro fragments. Compare with the
   repo backup to tell which is current.
3. **Rebuild `active/` in a third copy** with mtools: mark the broken
   `ACTIVE~1` entry and its long-name entry deleted (its cluster is 0 after
   fsck, so `mrd` would act on the root), `mdel` the `.rec` files, `mmd`
   `::/active`, `mcopy` the recovered files in with CRLF endings, and check
   with `fsck.vfat -n`.
4. **Write back only what changed** with
   [`scripts/patch-changed-sectors`](../scripts/patch-changed-sectors).
   Have the user run its dry run first, then `--write`. It refuses unless
   the device has the expected label, size and still matches the original
   copy in those sectors, and reads them back from the device afterwards.
5. The user closes the drive with Progm+F1 and checks the keys. Delete the
   images afterwards: the orphan chains can hold personal macro text.
