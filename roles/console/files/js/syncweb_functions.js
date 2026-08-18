// syncweb_functions.js
// Syncweb package management for IIAB Admin Console

function escHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// State
var syncwebCatalog = {};
var syncwebInstalled = {};
var syncwebDownloading = [];

function syncwebInit() {
  getSyncwebCatalog();
  displaySpaceAvail();
}

function getSyncwebCatalog() {
  var command = "GET-SYNCWEB-CAT";
  var cmd_args = {};
  cmd_args['timeout_secs'] = 10;
  var cmd = command + " " + JSON.stringify(cmd_args);
  sendCmdSrvCmd(cmd, procSyncwebCatalog, "GET-SYNCWEB-CATALOGUE");
}

function procSyncwebCatalog(data) {
  if (data === null || data === undefined) {
    $("#SyncwebPackageList").html(
      '<p class="text-warning">Unable to connect to syncweb daemon. ' +
      'Please check that the syncweb service is running.</p>'
    );
    return;
  }
  syncwebCatalog = {};
  if (data && data.packages) {
    for (var i = 0; i < data.packages.length; i++) {
      var pkg = data.packages[i];
      syncwebCatalog[pkg.collection_id] = pkg;
    }
  }
  renderSyncwebPackageList();
}

function renderSyncwebPackageList() {
  var html = '<table class="table table-striped">';
  html += '<thead><tr>';
  html += '<th style="width:3%"></th>';
  html += '<th style="width:25%">Name</th>';
  html += '<th style="width:8%">Version</th>';
  html += '<th style="width:8%">Language</th>';
  html += '<th style="width:12%">Size</th>';
  html += '<th style="width:44%">Description</th>';
  html += '</tr></thead><tbody>';

  for (var id in syncwebCatalog) {
    var pkg = syncwebCatalog[id];
    var lang = pkg.metadata ? (pkg.metadata.lang || '') : '';
    html += '<tr>';
    html += '<td><input type="checkbox" name="' + id + '"></td>';
    html += '<td>' + escHtml(pkg.name) + '</td>';
    html += '<td>' + escHtml(pkg.version) + '</td>';
    html += '<td>' + escHtml(lang) + '</td>';
    html += '<td>' + (pkg.total_size ? readableSize(pkg.total_size) : 'unknown') + '</td>';
    html += '<td>' + escHtml(pkg.description || '') + '</td>';
    html += '</tr>';
  }

  html += '</tbody></table>';
  if (Object.keys(syncwebCatalog).length === 0) {
    html = '<p>No packages found on the network. This may be because ' +
           'no peers are currently seeding packages, or the network ' +
           'connection is unavailable.</p>';
  }
  $("#SyncwebPackageList").html(html);

  make_button_disabled("#INST-SYNCWEB", Object.keys(syncwebCatalog).length === 0);
}

function instSyncwebPkgs() {
  var selected = [];
  $("#SyncwebPackageList input:checked").each(function() {
    selected.push({
      collection_id: this.name,
      ticket: syncwebCatalog[this.name].manifest_ticket
    });
  });

  if (selected.length === 0) return;

  for (var i = 0; i < selected.length; i++) {
    var pkg = syncwebCatalog[selected[i].collection_id];
    var cmd_args = {
      'ticket': selected[i].ticket,
      'target_dir': '/library/syncweb',
      'symlink_name': pkg.name || selected[i].collection_id
    };
    var cmd = "INST-SYNCWEB-PKG " + JSON.stringify(cmd_args);
    syncwebDownloading.push(selected[i].collection_id);
    sendCmdSrvCmd(cmd, genericCmdHandler, "", syncwebPkgError, cmd_args);
  }
  renderSyncwebPackageList();
}

function syncwebPkgError(data, cmd_args) {
  consoleLog("syncweb install failed for " + cmd_args.collection_id);
  var idx = syncwebDownloading.indexOf(cmd_args.collection_id);
  if (idx > -1) syncwebDownloading.splice(idx, 1);
  renderSyncwebPackageList();
}

function getSyncwebStat() {
  var command = "GET-SYNCWEB-STAT";
  return sendCmdSrvCmd(command, procSyncwebStat);
}

function procSyncwebStat(data) {
  syncwebInstalled = {};
  if (data && data.packages) {
    for (var i = 0; i < data.packages.length; i++) {
      var pkg = data.packages[i];
      syncwebInstalled[pkg.collection_id] = pkg;
    }
  }
  renderSyncwebInstalledList();
}

function renderSyncwebInstalledList() {
  var html = '';
  if (Object.keys(syncwebInstalled).length === 0) {
    html = '<p>No syncweb packages installed.</p>';
  } else {
    html += '<table class="table table-striped">';
    html += '<thead><tr>';
    html += '<th style="width:3%"></th>';
    html += '<th style="width:22%">Name</th>';
    html += '<th style="width:8%">Version</th>';
    html += '<th style="width:8%">Type</th>';
    html += '<th style="width:8%">Files</th>';
    html += '<th style="width:10%">Size</th>';
    html += '<th style="width:10%">Installed</th>';
    html += '<th style="width:31%">Path</th>';
    html += '</tr></thead><tbody>';

    for (var id in syncwebInstalled) {
      var pkg = syncwebInstalled[id];
      var use = pkg.metadata ? (pkg.metadata.intended_use || '') : '';
      html += '<tr>';
      html += '<td><input type="checkbox" name="' + id + '"></td>';
      html += '<td>' + escHtml(pkg.name) + '</td>';
      html += '<td>' + escHtml(pkg.version) + '</td>';
      html += '<td>' + escHtml(use) + '</td>';
      html += '<td>' + (pkg.file_count || 0) + '</td>';
      html += '<td>' + (pkg.total_size ? readableSize(pkg.total_size) : 'unknown') + '</td>';
      html += '<td>' + escHtml(pkg.installed_at || '') + '</td>';
      html += '<td>' + escHtml(pkg.installed_path || '') + '</td>';
      html += '</tr>';
    }

    html += '</tbody></table>';
  }
  $("#installedSyncwebPkgs").html(html);
}

function refreshSyncwebContentPanel() {
  $.when(getSyncwebStat())
    .done(renderSyncwebInstalledList, refreshDiskSpace);
}
