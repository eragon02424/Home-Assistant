async function searchFile() {
  const name = document.getElementById('search-name').value.trim();
  if (!name) return;
  const container = document.getElementById('search-results');
  container.innerHTML = '<p style="color:#9ca3af;font-size:0.85rem">Suche laeuft...</p>';
  const res = await fetch(apiUrl('/api/search'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filename: name})
  });
  const d = await res.json();
  if (!res.ok) { container.innerHTML = '<p style="color:#ef4444;font-size:0.85rem">Fehler: ' + (d.error||'unbekannt') + '</p>'; return; }
  if (d.found === 0) { container.innerHTML = '<p style="color:#f59e0b;font-size:0.85rem">Keine Treffer.</p>'; return; }
  container.innerHTML = '<p style="color:#9ca3af;font-size:0.78rem;margin-bottom:8px">' + d.found + ' Treffer:</p>';
  d.locations.forEach(function(loc) {
    var row = document.createElement('div');
    row.className = 'search-result-item';
    var info = document.createElement('div');
    info.innerHTML = '<div>' + loc.name + '</div><div class="path">' + loc.path + '</div>';
    var btn = document.createElement('button');
    btn.className = 'dl-btn';
    btn.textContent = String.fromCharCode(8659) + ' Download';
    btn.addEventListener('click', function() { downloadById(loc.item_id, loc.name); });
    row.appendChild(info);
    row.appendChild(btn);
    container.appendChild(row);
  });
}
