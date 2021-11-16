# Copyright (C) 2017 Red Hat, Inc.
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; If not, see <http://www.gnu.org/licenses/>.
#
# Author: Gris Ge <fge@redhat.com>

import os

from lsm import (uri_parse, search_property, LsmError, ErrorNumber, Client,
                 VERSION, IPlugin, NfsExport)

from hpsa_plugin import SmartArray
from arcconf_plugin import Arcconf


def _handle_errors(method):
    def _wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except LsmError:
            raise
        except Exception as common_error:
            raise LsmError(ErrorNumber.PLUGIN_BUG,
                           "Got unexpected error %s" % common_error)

    return _wrapper


class LocalPlugin(IPlugin):
    _KMOD_PLUGIN_MAP = {
        "megaraid_sas": "megaraid",
        "hpsa": "hpsa",
        "aacraid": "arcconf",
        "nfsd": "nfs",
    }

    def __init__(self):
        self._tmo_ms = 3000
        self.conns = []
        self.syss = []
        self.sys_con_map = {}
        self.unregistered = False
        self.nfs_conn = None

    def __del__(self):
        if not self.unregistered:
            self.plugin_unregister()

    def _query(self,
               query_func_name,
               search_key=None,
               search_value=None,
               flags=Client.FLAG_RSVD):
        lsm_objs = []
        if search_key == "system_id":
            if search_value not in self.sys_con_map.keys():
                return []
            return getattr(self.sys_con_map[search_value],
                           query_func_name)(flags=flags)

        for conn in self.conns:
            try:
                lsm_objs.extend(getattr(conn, query_func_name)(flags=flags))
            except LsmError as lsm_err:
                if lsm_err.code == ErrorNumber.NO_SUPPORT:
                    pass
                else:
                    raise
        return search_property(lsm_objs, search_key, search_value)

    def _exec(self, sys_id, func_name, parameters):
        if sys_id not in self.sys_con_map.keys():
            raise LsmError(ErrorNumber.NOT_FOUND_SYSTEM, "System not found")
        return getattr(self.sys_con_map[sys_id], func_name)(**parameters)

    @_handle_errors
    def plugin_register(self, uri, password, timeout, flags=Client.FLAG_RSVD):
        self._tmo_ms = timeout

        supported_plugins = set(LocalPlugin._KMOD_PLUGIN_MAP.values())
        if os.geteuid() != 0:
            raise LsmError(
                ErrorNumber.INVALID_ARGUMENT,
                "This plugin requires root privilege both daemon and client")
        uri_parsed = uri_parse(uri)
        uri_vars = uri_parsed.get("parameters", {})
        ignore_init_error = bool(uri_vars.get("ignore_init_error", "false"))

        sub_uri_paras = {}
        for plugin_name in supported_plugins:
            sub_uri_paras[plugin_name] = []

        for key in uri_vars.keys():
            for plugin_name in supported_plugins:
                if key.startswith("%s_" % plugin_name):
                    sub_uri_paras[plugin_name].append(
                        "%s=%s" %
                        (key[len("%s_" % plugin_name):], uri_vars[key]))

        only_plugin = uri_vars.get("only", "")
        if only_plugin and only_plugin not in supported_plugins:
            raise LsmError(
                ErrorNumber.INVALID_ARGUMENT,
                "Plugin defined in only=%s is not supported" % only_plugin)
        if only_plugin:
            requested_plugins = [only_plugin]
        else:
            # Check kernel module to determine which plugin to load
            requested_plugins = []
            cur_kmods = os.listdir("/sys/module/")
            for kmod_name, plugin_name in LocalPlugin._KMOD_PLUGIN_MAP.items():
                if kmod_name in cur_kmods:
                    requested_plugins.append(plugin_name)
            # smartpqi could be managed both by hpsa and arcconf plugin, hence
            # need extra care here: if arcconf binary tool is installed, we use
            # it, if not, we try hpsa binary tool. If none was installed, we
            # raise error generated by arcconf plugin.
            if "smartpqi" in cur_kmods:
                if Arcconf.find_arcconf():
                    requested_plugins.append("arcconf")
                elif SmartArray.find_sacli():
                    requested_plugins.append("hpsa")
                else:
                    # None was found, still use arcconf plugin which will
                    # generate proper error to user if ignore_init_error=false.
                    requested_plugins.append("arcconf")

            requested_plugins = set(requested_plugins)

        if not requested_plugins:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "No supported hardware found")

        for plugin_name in requested_plugins:
            plugin_uri = "%s://" % plugin_name
            if sub_uri_paras[plugin_name]:
                plugin_uri += "?%s" % "&".join(sub_uri_paras[plugin_name])
            try:
                conn = Client(plugin_uri, None, timeout, flags)
                # So far, no local plugins require password
                self.conns.append(conn)
                if plugin_name == 'nfs':
                    self.nfs_conn = conn
            except LsmError as lsm_err:
                if ignore_init_error:
                    pass
                else:
                    raise lsm_err
        for conn in self.conns:
            for sys in conn.systems():
                self.sys_con_map[sys.id] = conn
                self.syss.append(sys)
        if not self.sys_con_map:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "No supported systems found")

    @_handle_errors
    def plugin_unregister(self, flags=Client.FLAG_RSVD):
        for conn in self.conns:
            conn.plugin_unregister()
        self.unregistered = True

    @_handle_errors
    def job_status(self, job_id, flags=Client.FLAG_RSVD):
        raise LsmError(ErrorNumber.NO_SUPPORT, "Not supported yet")

    @_handle_errors
    def job_free(self, job_id, flags=Client.FLAG_RSVD):
        raise LsmError(ErrorNumber.NO_SUPPORT, "Not supported yet")

    @_handle_errors
    def plugin_info(self, flags=Client.FLAG_RSVD):
        return "Local Pseudo Plugin", VERSION

    @_handle_errors
    def time_out_set(self, ms, flags=Client.FLAG_RSVD):
        self._tmo_ms = ms
        for conn in self.conns:
            conn.time_out_set(ms, flags)

    @_handle_errors
    def time_out_get(self, flags=Client.FLAG_RSVD):
        return self._tmo_ms

    @_handle_errors
    def capabilities(self, system, flags=Client.FLAG_RSVD):
        return self._exec(system.id, "capabilities", {
            "system": system,
            "flags": flags
        })

    @_handle_errors
    def systems(self, flags=Client.FLAG_RSVD):
        return self.syss

    @_handle_errors
    def disks(self,
              search_key=None,
              search_value=None,
              flags=Client.FLAG_RSVD):
        return self._query("disks", search_key, search_value, flags)

    @_handle_errors
    def pools(self,
              search_key=None,
              search_value=None,
              flags=Client.FLAG_RSVD):
        return self._query("pools", search_key, search_value, flags)

    @_handle_errors
    def volumes(self,
                search_key=None,
                search_value=None,
                flags=Client.FLAG_RSVD):
        return self._query("volumes", search_key, search_value, flags)

    @_handle_errors
    def batteries(self,
                  search_key=None,
                  search_value=None,
                  flags=Client.FLAG_RSVD):
        return self._query("batteries", search_key, search_value, flags)

    @_handle_errors
    def volume_raid_info(self, volume, flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id, "volume_raid_info", {
            "volume": volume,
            "flags": flags
        })

    @_handle_errors
    def pool_member_info(self, pool, flags=Client.FLAG_RSVD):
        return self._exec(pool.system_id, "pool_member_info", {
            "pool": pool,
            "flags": flags
        })

    @_handle_errors
    def volume_raid_create_cap_get(self, system, flags=Client.FLAG_RSVD):
        return self._exec(system.id, "volume_raid_create_cap_get", {
            "system": system,
            "flags": flags
        })

    @_handle_errors
    def volume_raid_create(self,
                           name,
                           raid_type,
                           disks,
                           strip_size,
                           flags=Client.FLAG_RSVD):
        if not disks:
            raise LsmError(ErrorNumber.INVALID_ARGUMENT, "No disk defined")
        return self._exec(
            disks[0].system_id, "volume_raid_create", {
                "name": name,
                "raid_type": raid_type,
                "disks": disks,
                "strip_size": strip_size,
                "flags": flags
            })

    @_handle_errors
    def volume_cache_info(self, volume, flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id, "volume_cache_info", {
            "volume": volume,
            "flags": flags
        })

    @_handle_errors
    def volume_physical_disk_cache_update(self,
                                          volume,
                                          pdc,
                                          flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id,
                          "volume_physical_disk_cache_update", {
                              "volume": volume,
                              "pdc": pdc,
                              "flags": flags
                          })

    @_handle_errors
    def volume_write_cache_policy_update(self,
                                         volume,
                                         wcp,
                                         flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id, "volume_write_cache_policy_update",
                          {
                              "volume": volume,
                              "wcp": wcp,
                              "flags": flags
                          })

    @_handle_errors
    def volume_read_cache_policy_update(self,
                                        volume,
                                        rcp,
                                        flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id, "volume_read_cache_policy_update",
                          {
                              "volume": volume,
                              "rcp": rcp,
                              "flags": flags
                          })

    @_handle_errors
    def volume_delete(self, volume, flags=Client.FLAG_RSVD):
        return self._exec(volume.system_id, "volume_delete", {
            "volume": volume,
            "flags": flags
        })

    @_handle_errors
    def fs(self, search_key=None, search_value=None, flags=Client.FLAG_RSVD):
        return self._query("fs", search_key, search_value, flags)

    @_handle_errors
    def exports(self,
                search_key=None,
                search_value=None,
                flags=Client.FLAG_RSVD):
        if self.nfs_conn is not None:
            return self.nfs_conn.exports(search_key, search_value, flags)
        raise LsmError(
            ErrorNumber.NO_SUPPORT,
            "NFS plugin is not loaded, please start nfsd kernel "
            "module and related services")

    @_handle_errors
    def export_fs(self,
                  fs_id,
                  export_path,
                  root_list,
                  rw_list,
                  ro_list,
                  anon_uid=NfsExport.ANON_UID_GID_NA,
                  anon_gid=NfsExport.ANON_UID_GID_NA,
                  auth_type=None,
                  options=None,
                  flags=Client.FLAG_RSVD):
        if self.nfs_conn is not None:
            return self.nfs_conn.export_fs(fs_id, export_path, root_list,
                                           rw_list, ro_list, anon_uid,
                                           anon_gid, auth_type, options, flags)
        raise LsmError(
            ErrorNumber.NO_SUPPORT,
            "NFS plugin is not loaded, please load nfsd kernel "
            "module and related services")

    @_handle_errors
    def export_remove(self, export, flags=Client.FLAG_RSVD):
        if self.nfs_conn is not None:
            return self.nfs_conn.export_remove(export, flags)
        raise LsmError(
            ErrorNumber.NO_SUPPORT,
            "NFS plugin is not loaded, please load nfsd kernel "
            "module and related services")

    def export_auth(self, flags=Client.FLAG_RSVD):
        if self.nfs_conn is not None:
            return self.nfs_conn.export_auth(flags=flags)
        raise LsmError(
            ErrorNumber.NO_SUPPORT,
            "NFS plugin is not loaded, please load nfsd kernel "
            "module and related services")
