  const nameDiv = document.createElement('div');
  nameDiv.className = 'folder-name' + (enabled ? '' : ' disabled');
  nameDiv.textContent = String.fromCodePoint(0x1F4C1) + ' ' + folder.name;
  row.appendChild(nameDiv);
