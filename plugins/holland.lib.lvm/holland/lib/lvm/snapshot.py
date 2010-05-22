"""LVM Snapshot Lifecycle """

import os
import sys
import shutil
import logging
import tempfile
from holland.lib.lvm.base import LogicalVolume
from holland.lib.lvm.util import getmount, relpath, format_bytes

__all__ = [
    'SnapshotLifecycle'
]

LOG = logging.getLogger(__name__)

class LVMError(Exception):
    """LVMError"""

class SnapshotLifecycle(object):
    """A simple state machine to run through the LVM snapshot lifecycle"""
    tempdir = False

    def __init__(self,
                 target_directory,
                 snapshot_mountpoint=None,
                 snapshot_name=None,
                 snapshot_size=None):
        # track the target directory
        self.target_directory = os.path.realpath(target_directory)
        # logical_volume = handle to logical volume we're snapshotting
        self.logical_volume = None

        self.snapshot_mountpoint = snapshot_mountpoint
        if not self.snapshot_mountpoint:
            self.snapshot_mountpoint = tempfile.mkdtemp()
            self.tempdir = True

        self.snapshot_name = snapshot_name
        self.snapshot_size = snapshot_size
        # Placeholder for snapshot handle
        self.snapshot = None

    def start(self):
        """Start and initialize the snapshot process"""
        logging.info("Finding volume for directory: %s",
                     self.target_directory)
        mountpoint = getmount(self.target_directory)
        LOG.info("Mount point for directory: %s", mountpoint)
        self.logical_volume = LogicalVolume.find_mounted(mountpoint)
        if not self.logical_volume:
            raise LVMError("No logical volume found for "
                           "mountpoint=%r target_path=%r" % 
                           (mountpoint, self.target_directory))
        LOG.info("%r is on logical volume %s/%s",
                        self.target_directory,
                        self.logical_volume.vg_name,
                        self.logical_volume.lv_name)

        self.run_callback('init')
        return self.create_snapshot()

    def create_snapshot(self):
        """Snapshot the target LVM volume

        Before the snapshot is taken, a 'presnapshot'
        callback will be run.

        After the snapshot is taken, a postsnapshot
        callback will be run.
        """

        LOG.info("Running pre-snapshot tasks")
        self.run_callback('presnapshot')

        try:
            self.snapshot = self.logical_volume.snapshot(self.snapshot_name,
                                                         self.snapshot_size)
            LOG.info("Created snapshot /dev/%s/%s [%s] from logical volume"
                        " /dev/%s/%s",
                        self.snapshot.vg_name,
                        self.snapshot.lv_name,
                        format_bytes(int(self.snapshot.lv_size)),
                        self.logical_volume.vg_name,
                        self.logical_volume.lv_name)

        except EnvironmentError, exc: # covers LVMError,OSError, etc.
            # Log error
            # go to 'remove' state
            LOG.error("Failed to snapshot %s/%s: %s",
                         self.logical_volume.vg_name,
                         self.logical_volume.lv_name,
                         exc)
            return self.cleanup()
        LOG.info("Running post-snapshot tasks")
        self.run_callback('postsnapshot')
        return self.mount()

    def mount(self):
        """Mount the snapshotted LVM volume"""

        LOG.info("Running pre-mount tasks")
        self.run_callback('premount')
        # This may fail (OSError -> no such mountpoint, etc.)
        try:
            self.snapshot.mount(self.snapshot_mountpoint)
        except EnvironmentError, exc:
            LOG.error("Failed to mount %s/%s on %s: %s",
                         self.snapshot.vg_name,
                         self.snapshot.lv_name,
                         self.snapshot_mountpoint,
                         exc)
            return self.remove()
        return self.backup()

    def backup(self):
        """Backup the snapshotted LVM volume"""
        LOG.info("Running backup tasks")
        rpath = relpath(getmount(self.target_directory),
                        self.target_directory)
        source_directory = os.path.join(self.snapshot_mountpoint,
                                        rpath)
        self.run_callback('backup', os.path.realpath(source_directory))
        return self.unmount()

    def unmount(self):
        """Unmount the snapshotted LVM volume"""
        try:
            self.snapshot.unmount()
            LOG.info("Unmounted %s/%s",
                        self.snapshot.vg_name,
                        self.snapshot.lv_name)
        except EnvironmentError, exc:
            LOG.error("Failed to unmount snapshot: %s", exc)
            return self.cleanup()
        return self.remove()

    def remove(self):
        """Remove the LVM snapshot"""
        self.run_callback('preremove', self.snapshot)
        try:
            self.snapshot.refresh()
            total_snapshot_space = int(self.snapshot.lv_size)
            snap_percent_used = float(self.snapshot.snap_percent)
            used_snapshot_space = total_snapshot_space*(snap_percent_used/100.0)
            LOG.info("Final snapshot size %s [%.2f%% of %s]",
                format_bytes(used_snapshot_space),
                snap_percent_used,
                format_bytes(total_snapshot_space)
            )
        except LVMError, exc:
            LOG.info("Failed to refresh snapshot: %s", exc)

        try:
            self.snapshot.remove()
            LOG.info("Removed snapshot %s/%s",
                        self.snapshot.vg_name,
                        self.snapshot.lv_name)
            self.snapshot = None
        except AssertionError, exc:
            LOG.error("Failed to remove snapshot: %s", exc)

        return self.cleanup()


    def _check_for_existing_snapshot(self):
        """Check if an existing snapshot is alive"""
        if not self.snapshot and self.logical_volume:
            snapshot_lv = '%s/%s' % (self.logical_volume.vg_name,
                                     self.snapshot_name or
                                     self.logical_volume.lv_name + '_snapshot')
            LOG.info("Checking for outstanding snapshot: %s", snapshot_lv)
            try:
                self.snapshot = LogicalVolume.find_one(snapshot_lv)
            except ValueError:
                LOG.info("No outstanding snapshot found.")

    def cleanup(self):
        """Cleanup as much as possible in case of an error

        If this method is called in the context of a raised exception,
        that exception will be raised at the end of the cleanup.
        """

        self._check_for_existing_snapshot()

        if self.snapshot and self.snapshot.is_mounted():
            try:
                self.snapshot.unmount()
                logging.info("[cleanup] Unmounted snapshot %s", self.snapshot)
            except (KeyboardInterrupt, SystemExit):
                # These errors should be handled, and balanced with
                # cleanup
                pass
            except (LVMError, OSError), exc:
                logging.error("Failed to unmount snapshot on cleanup: %s", exc)

        if self.snapshot and self.snapshot.exists():
            try:
                self.snapshot.remove()
                logging.info("[cleanup] Removed snapshot %s", self.snapshot)
            except (LVMError, OSError), exc:
                logging.error("Failed to remove snapshot on cleanup: %s", exc)

        if self.tempdir and os.path.exists(self.snapshot_mountpoint):
            try:
                shutil.rmtree(self.snapshot_mountpoint)
                logging.info("[cleanup] removed temporary snapshot "
                             "mountpoint %s",
                             self.snapshot_mountpoint)
            except IOError, exc:
                LOG.error("Failed to remove tempfile generated snapshot "
                             "mountpoint: %s", exc)
        # reraise any exceptions that propagated to us
        if sys.exc_info() != (None, None, None):
            LOG.debug("Reraising exception", exc_info=True)
            raise


    def run(self):
        """Run through the lifecycle of an LVM snapshot backup"""
        try:
            self.start()
        except:
            self.cleanup()
            raise
