# noteshelf-apuntes

Convierte automáticamente los respaldos de **Noteshelf 3** (`.nsa`) que hay en Google Drive en
archivos legibles para cualquier persona o IA, dentro del mismo Drive:

```
Apuntes legibles/
├── Índice de apuntes            ← Google Doc: cuadernos y páginas editadas cada día
├── <subcarpeta>/
│   └── <cuaderno>/
│       ├── <cuaderno> — resumen   ← Google Doc: texto, OCR y fechas por página
│       ├── <cuaderno>.pdf         ← fondo + escritura a mano
│       └── <cuaderno> — p012.png  ← cada página escrita a mano
└── …
```

Corre en GitHub Actions cada hora (horario editable en `.github/workflows/sync.yml`) y solo reconvierte los
cuadernos cuyo `.nsa` cambió. También se puede lanzar a mano desde **Actions → Sincronizar apuntes → Run workflow**.

## Configuración (una sola vez, ~15 min)

### 1. Proyecto de Google Cloud
1. Entra a <https://console.cloud.google.com/> con tu cuenta de Gmail y crea un proyecto (p. ej. `noteshelf-apuntes`).
2. **APIs y servicios → Biblioteca →** busca **Google Drive API → Habilitar**.

### 2. Pantalla de consentimiento
1. **Google Auth Platform** (o *Pantalla de consentimiento de OAuth*) → **Comenzar**.
   Nombre de la app: `noteshelf-apuntes`, correo de asistencia: el tuyo, público: **Externo**.
2. En **Público (Audience)** pulsa **Publicar app** → *En producción*.
   ⚠️ Si se queda en "Prueba", el token deja de servir a los 7 días.
   No hace falta verificación: es solo para tu cuenta.

### 3. Credenciales OAuth
1. **Clientes → Crear cliente** → tipo **Aplicación web**.
2. En **URI de redireccionamiento autorizados** agrega exactamente:
   `https://developers.google.com/oauthplayground`
3. Crear y copia el **ID de cliente** y el **Secreto del cliente**.

### 4. Obtener el refresh token
1. Abre <https://developers.google.com/oauthplayground>.
2. ⚙️ (arriba a la derecha) → marca **Use your own OAuth credentials** → pega el ID y el secreto.
3. En *Step 1*, en la caja de abajo escribe estos dos permisos (separados por espacio) y pulsa **Authorize APIs**:
   ```
   https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file
   ```
4. Entra con tu Gmail. Google dirá *"Google no verificó esta app"* → **Configuración avanzada → Ir a noteshelf-apuntes** → Continuar.
5. *Step 2* → **Exchange authorization code for tokens** → copia el **Refresh token**.

Permisos: `drive.readonly` para leer los `.nsa`; `drive.file` para crear y editar **solo** los archivos que crea el script.
No puede modificar ni borrar nada más de tu Drive.

### 5. Secretos en GitHub
En este repo: **Settings → Secrets and variables → Actions → New repository secret**, crea:

| Nombre | Valor |
|---|---|
| `GOOGLE_CLIENT_ID` | ID de cliente |
| `GOOGLE_CLIENT_SECRET` | Secreto del cliente |
| `GOOGLE_REFRESH_TOKEN` | Refresh token |

Opcional (pestaña *Variables*, no son públicas): `NSA_TZ` (zona horaria de las fechas, p. ej. `America/New_York`;
por defecto UTC), `SOURCE_FOLDER_ID` (si la carpeta de respaldo no se llama `Noteshelf 3 Android`) y
`OUTPUT_FOLDER_NAME` (por defecto `Apuntes legibles`).

### 6. Primera corrida
**Actions → Sincronizar apuntes → Run workflow.** Al terminar aparece la carpeta `Apuntes legibles` en tu Drive.

## Notas
- En Noteshelf debe estar activo el **respaldo automático a Google Drive** (Ajustes → Nube y respaldo).
- El texto de la letra a mano aparece en el resumen si Noteshelf tiene activado el **reconocimiento de escritura**
  (idioma español). Si no, la página queda como imagen PNG.
- Si un cuaderno se borra en Noteshelf, desaparece del índice, pero sus archivos convertidos se conservan.
- Los registros de Actions no muestran nombres de cuadernos (en un repo público son visibles para cualquiera).
- Si una corrida falla, GitHub te avisa por correo. Lo más común es que el token se haya revocado: repite el paso 4.

## Uso local

```bash
pip install pymupdf
python3 nsa2pdf.py cuaderno.nsa -o salida/ --png 110 --solo-mano
```

Pruebas (Drive simulado): `python3 tests/test_sync.py cuaderno.nsa`

Formato `.nsa` documentado en el encabezado de `nsa2pdf.py` (basado en la ingeniería inversa de
[NSA_Convertor](https://github.com/adityachintala/NSA_Convertor)).
