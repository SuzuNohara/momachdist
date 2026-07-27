"""
Interfaz grafica del Inventario Betterware, con panel de pestanas:

 - Dashboard: resumen general (piezas en stock, ventas, alertas).
 - Inventario: existencias actuales, con buscador.
 - Pedidos: historial completo de remisiones cargadas, filtrable por
   producto, codigo, asociado o semana.
 - Ventas: historial de ventas registradas + boton para registrar una
   venta nueva.
 - Entregas Asociado: productos entregados a asociados, con status y
   forma(s) de pago administrables desde aqui (no en Excel).
 - Asociados: directorio de asociados (nombre, telefono, notas) con
   boton para abrir WhatsApp directo.

El Excel maestro se guarda siempre junto al programa, con el nombre
"inventario_betterware.xlsx", y funciona como respaldo/consulta: toda
la operacion del dia a dia se hace desde esta interfaz.
"""

import os
import sys
import logging
import subprocess
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from typing import Final

import backup
import core
import db
import pdf_extractor

logger = logging.getLogger(__name__)

APP_TITLE = "Inventario Betterware"
COLOR_MARCA = "#12C1B4"
COLOR_ROSA = "#E00176"
COLOR_AZUL = "#005EB8"

# Valores admitidos por el CHECK de `asociados.status` en el esquema. Se
# declaran aqui para que el combobox del formulario y la validacion de la capa
# core hablen exactamente del mismo vocabulario.
STATUS_ASOCIADO_OPCIONES = ("Activo", "Inactivo")

# `ventas.fecha` es `datetime('now','localtime')`, es decir 'AAAA-MM-DD HH:MM:SS'.
# Los filtros de la pestana de Ventas capturan solo la fecha, asi que las
# comparaciones se hacen sobre este prefijo y nunca sobre la hora.
LARGO_FECHA: Final[int] = 10


def ruta_base():
    """Carpeta donde vive el programa (funciona igual como .py o como .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def etiqueta_formulario(padre, texto, primera=False) -> tk.Label:
    """Rotulo de un campo en los dialogos de alta/edicion.

    Concentra el formato que antes se repetia literal en cada campo; `primera`
    solo cambia el margen superior del primer rotulo del dialogo.

    Time: O(1) | Space: O(1)
    """
    etiqueta = tk.Label(padre, text=texto, bg="#FFFFFF", font=("Arial", 10))
    etiqueta.pack(anchor="w", padx=20, pady=(20 if primera else 15, 0))
    return etiqueta


EXCEL_PATH = os.path.join(ruta_base(), "inventario_betterware.xlsx")

# Rutas de la capa de resiliencia (RT-5). Se resuelven con los helpers de
# `backup`, que a su vez usan `db.ruta_base()` -- nunca el directorio actual.
DB_PATH = str(backup.ruta_db())
LOG_PATH = str(backup.ruta_log())


# ======================================================================
# Ventana principal
# ======================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        backup.startup(DB_PATH, LOG_PATH)
        # Conexion unica de la sesion, inyectada en cada llamada a `core`
        # (ADR-2: la capa core nunca abre conexiones por su cuenta). `init_db`
        # es idempotente, asi que sirve de arranque y de migracion en un paso.
        self.conn = db.init_db(DB_PATH)
        self.title(APP_TITLE)
        self.geometry("1150x720")
        self.configure(bg="#FFFFFF")

        self._construir_barra_superior()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        self.tab_dashboard = TabDashboard(self.notebook, self)
        self.tab_inventario = TabInventario(self.notebook, self)
        self.tab_pedidos = TabPedidos(self.notebook, self)
        self.tab_ventas = TabVentas(self.notebook, self)
        self.tab_entregas = TabEntregas(self.notebook, self)
        self.tab_asociados = TabAsociados(self.notebook, self)
        self.tab_clientes = TabClientes(self.notebook, self)

        self.notebook.add(self.tab_dashboard, text="  📊 Dashboard  ")
        self.notebook.add(self.tab_inventario, text="  📦 Inventario  ")
        self.notebook.add(self.tab_pedidos, text="  🧾 Pedidos  ")
        self.notebook.add(self.tab_ventas, text="  🛒 Ventas  ")
        self.notebook.add(self.tab_entregas, text="  🤝 Entregas Asociado  ")
        self.notebook.add(self.tab_asociados, text="  👥 Asociados  ")
        self.notebook.add(self.tab_clientes, text="  👥 Clientes  ")

        self.status_bar = tk.Label(
            self, text="", font=("Arial", 9), bg="#F5F5F5", fg="#444444",
            anchor="w", padx=10, pady=4,
        )
        self.status_bar.pack(fill="x", side="bottom")

        self.refrescar_todo()

    def _construir_barra_superior(self):
        barra = tk.Frame(self, bg=COLOR_MARCA, height=55)
        barra.pack(fill="x", side="top")

        tk.Label(
            barra, text="Inventario Betterware", font=("Arial", 15, "bold"),
            bg=COLOR_MARCA, fg="white", padx=15,
        ).pack(side="left", pady=10)

        tk.Button(
            barra, text="📄 Cargar PDF(s)", font=("Arial", 10, "bold"),
            bg="white", fg=COLOR_MARCA, relief="flat", padx=12, pady=6,
            command=self.abrir_flujo_carga_pdf,
        ).pack(side="left", padx=8, pady=10)

        tk.Button(
            barra, text="🛒 Registrar venta", font=("Arial", 10, "bold"),
            bg="white", fg=COLOR_ROSA, relief="flat", padx=12, pady=6,
            command=self.abrir_ventana_venta,
        ).pack(side="left", padx=8, pady=10)

        tk.Button(
            barra, text="🔄 Actualizar vistas", font=("Arial", 10),
            bg=COLOR_MARCA, fg="white", relief="flat", padx=12, pady=6,
            command=self.refrescar_todo, highlightthickness=0, bd=0,
        ).pack(side="left", padx=8, pady=10)

        tk.Button(
            barra, text="📁 Abrir Excel", font=("Arial", 10),
            bg=COLOR_MARCA, fg="white", relief="flat", padx=12, pady=6,
            command=self.abrir_excel, highlightthickness=0, bd=0,
        ).pack(side="right", padx=12, pady=10)

    # ------------------------------------------------------------------
    # Acciones centrales (usadas por varias pestanas)
    # ------------------------------------------------------------------

    def mostrar_status(self, texto, color="#008000"):
        self.status_bar.config(text=texto, fg=color)

    def refrescar_todo(self):
        self.tab_dashboard.refrescar()
        self.tab_inventario.refrescar()
        self.tab_pedidos.refrescar()
        self.tab_ventas.refrescar()
        self.tab_entregas.refrescar()
        self.tab_asociados.refrescar()
        self.tab_clientes.refrescar()

    def abrir_flujo_carga_pdf(self):
        archivos = filedialog.askopenfilenames(
            title="Selecciona una o varias remisiones (PDF)",
            filetypes=[("Archivos PDF", "*.pdf")],
        )
        if not archivos:
            return

        self.mostrar_status("Leyendo PDF(s), espera un momento...", "#666666")
        self.update_idletasks()

        try:
            filas, errores = pdf_extractor.preparar_filas_desde_pdfs(list(archivos))
        except Exception as e:
            logger.exception("Fallo la lectura de %d PDF(s)", len(archivos))
            messagebox.showerror(APP_TITLE, f"Ocurrió un error inesperado:\n{e}")
            self.mostrar_status("")
            return

        if not filas:
            mensaje = "No se encontró ningún producto en los archivos seleccionados."
            if errores:
                mensaje += "\n\n" + "\n".join(errores)
            self.mostrar_status(mensaje, "#CC0000")
            return

        self.mostrar_status("")
        VentanaPrevisualizacion(self, filas, errores)

    def al_confirmar_carga(self, filas_confirmadas):
        try:
            core.confirmar_carga(self.conn, filas_confirmadas)
        except Exception as e:
            logger.exception("Fallo el guardado de la carga confirmada")
            messagebox.showerror(APP_TITLE, f"Ocurrió un error al guardar:\n{e}")
            return

        self.mostrar_status(f"Listo. Se agregaron {len(filas_confirmadas)} producto(s) al inventario.")
        self.refrescar_todo()
        messagebox.showinfo(APP_TITLE, "El inventario se actualizó correctamente.")

    def abrir_ventana_venta(self, codigo_preseleccionado=None):
        """Abre la ventana de venta contra la base, ya no contra el Excel.

        La guarda anterior era `os.path.exists(EXCEL_PATH)`: tras la migracion
        a SQLite habria dejado la venta inalcanzable en una instalacion limpia,
        que es justo cuando no hay xlsx. El aviso de "todavia no hay
        inventario" ahora sale de la propia lista de productos, que se llena
        desde `vw_existencias`.
        """
        VentanaVenta(self, codigo_preseleccionado=codigo_preseleccionado)

    def abrir_excel(self):
        if not os.path.exists(EXCEL_PATH):
            messagebox.showinfo(
                APP_TITLE,
                "Todavía no existe el Excel. Primero procesa al menos un PDF.",
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(EXCEL_PATH)
            elif sys.platform == "darwin":
                subprocess.call(["open", EXCEL_PATH])
            else:
                subprocess.call(["xdg-open", EXCEL_PATH])
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"No se pudo abrir el archivo:\n{e}")


# ======================================================================
# Pestana: Dashboard
# ======================================================================

class TabDashboard(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app

        tk.Label(self, text="Resumen general", font=("Arial", 16, "bold"), fg=COLOR_MARCA).pack(pady=(20, 15))

        self.tarjetas_frame = tk.Frame(self)
        self.tarjetas_frame.pack(fill="x", padx=30)

        self.labels_tarjetas = {}
        self._crear_tarjetas()

        tk.Label(self, text="Productos con poco inventario", font=("Arial", 12, "bold"), fg="#CC0000").pack(pady=(25, 5))

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=30, pady=(0, 20))
        columnas = ("codigo", "descripcion", "disponibles")
        self.tree_bajo_stock = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=8)
        self.tree_bajo_stock.heading("codigo", text="Código")
        self.tree_bajo_stock.heading("descripcion", text="Descripción")
        self.tree_bajo_stock.heading("disponibles", text="Piezas disponibles")
        self.tree_bajo_stock.column("codigo", width=90, anchor="center")
        self.tree_bajo_stock.column("descripcion", width=350, anchor="w")
        self.tree_bajo_stock.column("disponibles", width=140, anchor="center")
        self.tree_bajo_stock.pack(fill="both", expand=True)

    def _crear_tarjetas(self):
        datos = [
            ("piezas_disponibles", "Piezas en stock"),
            ("valor_inventario_costo", "Valor de inventario ($)"),
            ("num_ventas", "Ventas registradas"),
            ("ganancia_total", "Ganancia total ($)"),
            ("num_pedidos_distintos", "Pedidos cargados"),
            ("monto_pendiente_asociados", "Por cobrar a asociados ($)"),
        ]
        for i, (clave, titulo) in enumerate(datos):
            tarjeta = tk.Frame(self.tarjetas_frame, bg="#F5F5F5", bd=1, relief="solid")
            tarjeta.grid(row=i // 3, column=i % 3, padx=8, pady=8, sticky="nsew")
            self.tarjetas_frame.grid_columnconfigure(i % 3, weight=1)

            valor_label = tk.Label(tarjeta, text="0", font=("Arial", 20, "bold"), bg="#F5F5F5", fg=COLOR_MARCA)
            valor_label.pack(pady=(15, 0))
            tk.Label(tarjeta, text=titulo, font=("Arial", 10), bg="#F5F5F5", fg="#666666").pack(pady=(0, 15))
            self.labels_tarjetas[clave] = valor_label

    def refrescar(self):
        resumen = core.obtener_resumen_dashboard(self.app.conn)

        for clave, label in self.labels_tarjetas.items():
            valor = resumen.get(clave, 0)
            if clave in ("valor_inventario_costo", "ganancia_total", "monto_pendiente_asociados"):
                label.config(text=f"${float(valor):,.2f}")
            else:
                label.config(text=f"{int(valor):,}")

        self.tree_bajo_stock.delete(*self.tree_bajo_stock.get_children())
        for prod in resumen.get("productos_bajo_stock", []):
            self.tree_bajo_stock.insert("", "end", values=(
                prod["Codigo articulo"], prod["Descripcion"], int(prod["Piezas disponibles"])
            ))


# ======================================================================
# Pestana: Inventario (Existencias)
# ======================================================================

class TabInventario(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self.catalogo_completo = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)
        tk.Label(barra, text="Buscar producto o código:", font=("Arial", 10)).pack(side="left")
        self.busqueda_var = tk.StringVar()
        self.busqueda_var.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.busqueda_var, font=("Arial", 10), width=30).pack(side="left", padx=8)

        tk.Button(
            barra, text="🛒 Registrar venta de este producto", font=("Arial", 9, "bold"),
            bg=COLOR_ROSA, fg="white", relief="flat", padx=10, pady=4,
            command=self._vender_seleccionado,
        ).pack(side="right")

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("codigo", "descripcion", "recibidas", "vendidas", "disponibles", "costo")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "codigo": "Código", "descripcion": "Descripción", "recibidas": "Recibidas",
            "vendidas": "Vendidas", "disponibles": "Disponibles", "costo": "Costo unitario",
        }
        anchos = {"codigo": 80, "descripcion": 320, "recibidas": 90, "vendidas": 90, "disponibles": 100, "costo": 110}
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="center" if col != "descripcion" else "w")

        self.tree.tag_configure("bajo_stock", background="#FFC7CE")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self):
        self.catalogo_completo = core.obtener_existencias(self.app.conn)
        self._aplicar_filtro()

    def _aplicar_filtro(self):
        texto = self.busqueda_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for prod in self.catalogo_completo:
            codigo = str(prod["Codigo articulo"]).lower()
            desc = str(prod["Descripcion"]).lower()
            if texto and texto not in codigo and texto not in desc:
                continue
            disponibles = int(prod["Piezas disponibles"])
            tag = ("bajo_stock",) if disponibles <= core.STOCK_BAJO_UMBRAL else ()
            self.tree.insert("", "end", iid=str(prod["Codigo articulo"]), values=(
                prod["Codigo articulo"], prod["Descripcion"], int(prod["Piezas recibidas"]),
                int(prod["Piezas vendidas"]), disponibles, f"${prod['Precio unitario costo']:.2f}",
            ), tags=tag)

    def _vender_seleccionado(self):
        seleccion = self.tree.selection()
        if not seleccion:
            messagebox.showinfo(APP_TITLE, "Selecciona un producto de la tabla primero.")
            return
        self.app.abrir_ventana_venta(codigo_preseleccionado=seleccion[0])


# ======================================================================
# Pestana: Pedidos (Movimientos), filtrable
# ======================================================================

class TabPedidos(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self.datos_completos = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)

        tk.Label(barra, text="Producto/código:", font=("Arial", 10)).pack(side="left")
        self.filtro_producto = tk.StringVar()
        self.filtro_producto.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_producto, font=("Arial", 10), width=20).pack(side="left", padx=(5, 15))

        tk.Label(barra, text="Asociado:", font=("Arial", 10)).pack(side="left")
        self.filtro_asociado = tk.StringVar()
        self.filtro_asociado.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_asociado, font=("Arial", 10), width=18).pack(side="left", padx=(5, 15))

        tk.Label(barra, text="Semana:", font=("Arial", 10)).pack(side="left")
        self.filtro_semana = tk.StringVar(value="Todas")
        self.combo_semana = ttk.Combobox(barra, textvariable=self.filtro_semana, state="readonly", width=12, values=["Todas"])
        self.combo_semana.pack(side="left", padx=(5, 15))
        self.combo_semana.bind("<<ComboboxSelected>>", lambda *_: self._aplicar_filtro())

        tk.Label(barra, text="Folio:", font=("Arial", 10)).pack(side="left")
        self.filtro_folio = tk.StringVar()
        self.filtro_folio.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_folio, font=("Arial", 10), width=16).pack(side="left", padx=5)

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("semana", "folio", "codigo", "descripcion", "asociado", "cant_surtida", "asoc", "casa", "local", "costo")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "semana": "Semana", "folio": "Folio", "codigo": "Código", "descripcion": "Descripción",
            "asociado": "Nombre asociado", "cant_surtida": "Cant.", "asoc": "→Asoc.", "casa": "→Casa",
            "local": "→Local", "costo": "Precio que pagas",
        }
        anchos = {
            "semana": 75, "folio": 110, "codigo": 65, "descripcion": 190, "asociado": 150,
            "cant_surtida": 50, "asoc": 55, "casa": 50, "local": 55, "costo": 100,
        }
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="center" if col != "descripcion" and col != "asociado" else "w")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self) -> None:
        """Recarga el historial desde SQLite y rearma el combo de semanas.

        La lectura va a `core.obtener_movimientos(conn)` con la conexion unica de
        la sesion (ADR-2): ya no hay maestro de Excel ni guarda
        `os.path.exists`, una base recien creada simplemente devuelve `[]`. Las
        claves de cada dict son las mismas que entregaba el Excel, asi que
        `_aplicar_filtro` sigue sirviendo sin cambios.

        Time: O(n log n) por el `sorted` de semanas | Space: O(n)
        """
        self.datos_completos = core.obtener_movimientos(self.app.conn)

        semanas = sorted({str(f.get("Semana", "")) for f in self.datos_completos if f.get("Semana")})
        self.combo_semana["values"] = ["Todas"] + semanas
        if self.filtro_semana.get() not in self.combo_semana["values"]:
            self.filtro_semana.set("Todas")

        self._aplicar_filtro()

    def _aplicar_filtro(self):
        texto_prod = self.filtro_producto.get().strip().lower()
        texto_asoc = self.filtro_asociado.get().strip().lower()
        texto_folio = self.filtro_folio.get().strip().lower()
        semana_sel = self.filtro_semana.get()

        self.tree.delete(*self.tree.get_children())
        for fila in self.datos_completos:
            if texto_prod and texto_prod not in str(fila.get("Codigo articulo", "")).lower() \
                    and texto_prod not in str(fila.get("Descripcion", "")).lower():
                continue
            if texto_asoc and texto_asoc not in str(fila.get("Nombre asociado", "")).lower():
                continue
            if texto_folio and texto_folio not in str(fila.get("Folio de pedido", "")).lower():
                continue
            if semana_sel != "Todas" and str(fila.get("Semana", "")) != semana_sel:
                continue

            self.tree.insert("", "end", values=(
                fila.get("Semana", ""), fila.get("Folio de pedido", ""), fila.get("Codigo articulo", ""),
                fila.get("Descripcion", ""), fila.get("Nombre asociado", ""), fila.get("Cantidad surtida", 0),
                fila.get("Cantidad Asociado", 0), fila.get("Cantidad Casa", 0), fila.get("Cantidad Local", 0),
                f"${float(fila.get('Precio que pagas', 0) or 0):.2f}",
            ))


# ======================================================================
# Pestana: Ventas
# ======================================================================

def _pasa_filtro_producto(venta: dict, texto: str) -> bool:
    """Indica si la venta casa con `texto` en su codigo o su descripcion (R9).

    La comparacion es por substring y sin distinguir mayusculas; un texto en
    blanco no descarta nada.

    Time: O(n) sobre la longitud de los campos | Space: O(n)
    """
    if not texto:
        return True
    return (
        texto in str(venta.get("codigo", "")).lower()
        or texto in str(venta.get("descripcion", "")).lower()
    )


def _pasa_filtro_fecha(venta: dict, desde: str, hasta: str) -> bool:
    """Indica si la venta cae dentro del rango `desde`-`hasta`, inclusive (R10).

    `ventas.fecha` se guarda como `'YYYY-MM-DD HH:MM:SS'`, asi que solo se
    comparan los diez primeros caracteres: contra la fecha completa, un `hasta`
    de hoy dejaria fuera todas las ventas de hoy por su parte horaria. Un
    extremo en blanco no acota ese lado.

    Time: O(1) | Space: O(1)
    """
    dia = str(venta.get("fecha", ""))[:LARGO_FECHA]
    if desde and dia < desde:
        return False
    if hasta and dia > hasta:
        return False
    return True


def _fila_visible_venta(venta: dict) -> tuple:
    """Valores de una fila del historial en el orden de las columnas (R11, R12).

    `cliente` ya llega resuelto por el core (`'Mostrador'` cuando la venta no
    tiene cliente). `saldo` solo se pinta cuando queda algo por cobrar: una
    venta saldada muestra la celda vacia en vez de un `$0.00` que distrae.

    Time: O(1) | Space: O(1)
    """
    saldo = float(venta.get("saldo_pendiente", 0) or 0)
    return (
        venta.get("fecha", ""), venta.get("cliente", ""), venta.get("codigo", ""),
        venta.get("descripcion", ""), venta.get("cantidad", 0),
        f"${float(venta.get('precio_costo', 0) or 0):.2f}",
        f"${float(venta.get('precio_publico', 0) or 0):.2f}",
        f"${float(venta.get('total', 0) or 0):.2f}",
        f"${float(venta.get('ganancia', 0) or 0):.2f}",
        f"${saldo:.2f}" if saldo > 0 else "",
    )


class TabVentas(ttk.Frame):
    """Historial de ventas leido de SQLite, filtrable por producto y por fecha.

    Las filas vienen de `core.obtener_ventas_historial` (una por linea vendida)
    y traen el nombre del cliente y el saldo pendiente ya calculados. Los dos
    filtros --producto/codigo y rango de fechas-- se combinan con AND y corren
    **en memoria** sobre `datos_completos`: cambiar un filtro nunca vuelve a
    consultar la base (CLI-05 R8-R12).
    """

    def __init__(self, notebook, app) -> None:
        super().__init__(notebook)
        self.app = app
        self.datos_completos = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)
        tk.Label(barra, text="Producto/código:", font=("Arial", 10)).pack(side="left")
        self.filtro_var = tk.StringVar()
        self.filtro_var.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_var, font=("Arial", 10), width=25).pack(side="left", padx=(5, 15))

        tk.Label(barra, text="Desde (AAAA-MM-DD):", font=("Arial", 10)).pack(side="left")
        self.filtro_desde = tk.StringVar()
        self.filtro_desde.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_desde, font=("Arial", 10), width=12).pack(side="left", padx=(5, 15))

        tk.Label(barra, text="Hasta:", font=("Arial", 10)).pack(side="left")
        self.filtro_hasta = tk.StringVar()
        self.filtro_hasta.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_hasta, font=("Arial", 10), width=12).pack(side="left", padx=5)

        tk.Button(
            barra, text="🛒 Registrar venta", font=("Arial", 10, "bold"),
            bg=COLOR_ROSA, fg="white", relief="flat", padx=12, pady=6,
            command=self.app.abrir_ventana_venta,
        ).pack(side="right")

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = (
            "fecha", "cliente", "codigo", "descripcion", "cantidad",
            "costo", "publico", "total", "ganancia", "saldo",
        )
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "fecha": "Fecha", "cliente": "Cliente", "codigo": "Código",
            "descripcion": "Descripción", "cantidad": "Cant.", "costo": "Costo",
            "publico": "Precio público", "total": "Total", "ganancia": "Ganancia",
            "saldo": "Saldo pendiente",
        }
        anchos = {
            "fecha": 130, "cliente": 140, "codigo": 65, "descripcion": 180, "cantidad": 50,
            "costo": 80, "publico": 90, "total": 80, "ganancia": 80, "saldo": 110,
        }
        alineados_izquierda = {"descripcion", "cliente"}
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="w" if col in alineados_izquierda else "center")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self) -> None:
        """Recarga el historial desde SQLite y repinta la tabla (R15, R8).

        La lectura va a `core.obtener_ventas_historial(self.app.conn)` con la
        conexion unica de la sesion (ADR-2): ya no hay maestro de Excel ni
        guarda `os.path.exists`, una base sin ventas devuelve `[]`. Las claves
        de cada dict son las del contrato nuevo (minusculas), no las del Excel.

        Time: O(n) sobre las lineas vendidas | Space: O(n)
        """
        self.datos_completos = core.obtener_ventas_historial(self.app.conn)
        self._aplicar_filtro()

    def _aplicar_filtro(self) -> None:
        """Repinta la tabla con las filas que pasan producto **y** fecha (R9-R12).

        Los dos predicados se combinan con AND y se evaluan sobre la lista ya
        cargada: no hay ninguna consulta por fila (sin N+1).

        Time: O(n) sobre las filas cargadas | Space: O(1)
        """
        texto = self.filtro_var.get().strip().lower()
        desde = self.filtro_desde.get().strip()
        hasta = self.filtro_hasta.get().strip()

        self.tree.delete(*self.tree.get_children())
        for venta in self.datos_completos:
            if not _pasa_filtro_producto(venta, texto):
                continue
            if not _pasa_filtro_fecha(venta, desde, hasta):
                continue
            self.tree.insert("", "end", values=_fila_visible_venta(venta))


# ======================================================================
# Pestana: Entregas Asociado
# ======================================================================

class TabEntregas(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self.datos_completos = []

        tk.Label(
            self, text="Doble clic sobre una entrega para actualizar su status o registrar el pago.",
            font=("Arial", 9), fg="#666666",
        ).pack(pady=(15, 5))

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("fecha", "folio", "codigo", "descripcion", "cantidad", "monto", "status", "pago1", "monto1", "pago2", "monto2")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "fecha": "Fecha", "folio": "Folio", "codigo": "Código", "descripcion": "Descripción",
            "cantidad": "Cant.", "monto": "Debe pagar", "status": "Status", "pago1": "Forma pago 1",
            "monto1": "Monto 1", "pago2": "Forma pago 2", "monto2": "Monto 2",
        }
        anchos = {
            "fecha": 110, "folio": 110, "codigo": 65, "descripcion": 170, "cantidad": 50,
            "monto": 85, "status": 140, "pago1": 95, "monto1": 75, "pago2": 95, "monto2": 75,
        }
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="center" if col != "descripcion" else "w")

        self.tree.tag_configure("pagado", background="#D4EDDA")
        self.tree.tag_configure("pendiente_pago", background="#FFF3CD")
        self.tree.tag_configure("pendiente_recoger", background="#FFC7CE")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._abrir_detalle)

    def refrescar(self):
        if not os.path.exists(EXCEL_PATH):
            self.datos_completos = []
        else:
            self.datos_completos = core.obtener_entregas_asociado(EXCEL_PATH)

        self.tree.delete(*self.tree.get_children())
        for entrega in self.datos_completos:
            status = entrega.get("Status", "")
            if status == "Pagado":
                tag = "pagado"
            elif status == "Recogido - no pagado":
                tag = "pendiente_pago"
            else:
                tag = "pendiente_recoger"

            self.tree.insert("", "end", iid=str(entrega["_indice"]), tags=(tag,), values=(
                entrega.get("Fecha entrega", ""), entrega.get("Folio de pedido", ""), entrega.get("Codigo", ""),
                entrega.get("Descripcion", ""), entrega.get("Cantidad entregada", 0),
                f"${float(entrega.get('Monto que debe pagar', 0) or 0):.2f}", status,
                entrega.get("Forma de pago 1", "") or "", entrega.get("Monto 1", "") or "",
                entrega.get("Forma de pago 2", "") or "", entrega.get("Monto 2", "") or "",
            ))

    def _abrir_detalle(self, event):
        seleccion = self.tree.selection()
        if not seleccion:
            return
        indice = int(seleccion[0])
        entrega = next((e for e in self.datos_completos if e["_indice"] == indice), None)
        if entrega:
            VentanaDetalleEntrega(self.app, entrega)


# ======================================================================
# Pestana: Asociados (directorio)
# ======================================================================

class TabAsociados(ttk.Frame):
    """Directorio de asociados leido de SQLite (MERC-07).

    El `iid` de cada fila del Treeview es el `asociados.id` real, no el indice
    de fila: es lo que permite que Agregar/Editar/Eliminar operen por clave
    primaria estable aunque el orden del listado cambie entre refrescos.
    """

    def __init__(self, notebook, app) -> None:
        super().__init__(notebook)
        self.app = app
        # Cache del ultimo listado: evita releer la base para resolver la fila
        # seleccionada (anti N+1, `.langs/python.md` 4).
        self.asociados = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)

        tk.Button(
            barra, text="➕ Agregar asociado", font=("Arial", 10, "bold"),
            bg=COLOR_MARCA, fg="white", relief="flat", padx=12, pady=6,
            command=self._agregar,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            barra, text="✏️ Editar", font=("Arial", 10),
            bg="#EEEEEE", relief="flat", padx=12, pady=6, command=self._editar,
        ).pack(side="left", padx=8)

        tk.Button(
            barra, text="🗑️ Eliminar", font=("Arial", 10),
            bg="#EEEEEE", relief="flat", padx=12, pady=6, command=self._eliminar,
        ).pack(side="left", padx=8)

        tk.Button(
            barra, text="💬 Enviar WhatsApp", font=("Arial", 10, "bold"),
            bg="#25D366", fg="white", relief="flat", padx=12, pady=6,
            command=self._enviar_whatsapp,
        ).pack(side="right")

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("nombre", "telefono", "status", "saldo", "notas")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "nombre": "Nombre", "telefono": "Teléfono", "status": "Estado",
            "saldo": "Saldo pendiente", "notas": "Notas",
        }
        anchos = {"nombre": 200, "telefono": 130, "status": 90, "saldo": 120, "notas": 280}
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            alineacion = "w" if col in ("nombre", "notas") else "center"
            self.tree.column(col, width=anchos[col], anchor=alineacion)

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self) -> None:
        """Repuebla la tabla desde `core.listar_asociados` (R8).

        Una sola lectura trae nombre, telefono, estado, saldo y notas: el saldo
        lo mantienen los triggers del esquema (ADR-3), asi que no hay consulta
        por asociado.

        Time: O(n) sobre los asociados | Space: O(n)
        """
        self.tree.delete(*self.tree.get_children())
        self.asociados = core.listar_asociados(self.app.conn)
        for fila in self.asociados:
            self.tree.insert("", "end", iid=str(fila["id"]), values=(
                fila.get("nombre", ""),
                fila.get("telefono", "") or "",
                fila.get("status", "") or "",
                f"${float(fila.get('saldo_pendiente', 0) or 0):.2f}",
                fila.get("notas", "") or "",
            ))

    def _id_seleccionado(self) -> int | None:
        """`asociados.id` de la fila seleccionada, o `None` si no hay ninguna.

        Time: O(1) | Space: O(1)
        """
        seleccion = self.tree.selection()
        if not seleccion:
            messagebox.showinfo(APP_TITLE, "Selecciona un asociado de la lista primero.")
            return None
        return int(seleccion[0])

    def _datos(self, asociado_id):
        """Fila cacheada del asociado, o `None` si el listado ya no la tiene.

        Time: O(n) sobre el listado en memoria | Space: O(1)
        """
        return next((f for f in self.asociados if int(f["id"]) == asociado_id), None)

    def _agregar(self) -> None:
        VentanaAsociadoForm(self.app, modo="agregar")

    def _editar(self) -> None:
        asociado_id = self._id_seleccionado()
        if asociado_id is None:
            return
        datos = self._datos(asociado_id)
        if datos is None:
            return
        VentanaAsociadoForm(self.app, modo="editar", asociado_id=asociado_id, datos=datos)

    def _eliminar(self) -> None:
        """Da de baja al asociado seleccionado, mostrando el motivo si se niega.

        `core.eliminar_asociado` levanta `AsociadoError` cuando las FKs de
        entregas o de detalle protegen la fila: se registra en el log y se
        traduce a un `messagebox`, nunca escapa a la capa de presentacion
        (DEUDA-02).

        Time: O(1) | Space: O(1)
        """
        asociado_id = self._id_seleccionado()
        if asociado_id is None:
            return
        if not messagebox.askyesno(APP_TITLE, "¿Eliminar este asociado del directorio?"):
            return

        try:
            core.eliminar_asociado(self.app.conn, asociado_id)
        except core.AsociadoError as e:
            logger.exception("Fallo la baja del asociado id=%s", asociado_id)
            messagebox.showerror(APP_TITLE, str(e))
            return

        self.app.refrescar_todo()

    def _enviar_whatsapp(self) -> None:
        """Abre WhatsApp Web con el telefono del asociado seleccionado.

        `pdf_extractor.link_whatsapp` devuelve `None` cuando el telefono no deja
        digitos utiles: en ese caso se avisa y no se abre el navegador.

        Time: O(n) sobre el listado en memoria | Space: O(1)
        """
        asociado_id = self._id_seleccionado()
        if asociado_id is None:
            return
        datos = self._datos(asociado_id)
        if datos is None:
            return

        telefono = str(datos.get("telefono", "") or "")
        if pdf_extractor.link_whatsapp(telefono) is None:
            messagebox.showwarning(APP_TITLE, "Este asociado no tiene teléfono registrado.")
            return

        mensaje = simpledialog.askstring(
            "Mensaje de WhatsApp",
            f"Mensaje para {datos.get('nombre', '')} (opcional):",
        )
        webbrowser.open(pdf_extractor.link_whatsapp(telefono, mensaje or ""))


# ======================================================================
# Dialogo: agregar/editar asociado
# ======================================================================

class VentanaAsociadoForm(tk.Toplevel):
    """Alta/edicion de un asociado contra la capa core (R9).

    En modo editar recibe el `asociados.id` real y las claves del dict que
    entrega `core.listar_asociados` (`nombre`, `telefono`, `notas`, `status`).
    """

    def __init__(self, app, modo="agregar", asociado_id=None, datos=None) -> None:
        super().__init__(app)
        self.app = app
        self.modo = modo
        self.asociado_id = asociado_id
        self.title("Agregar asociado" if modo == "agregar" else "Editar asociado")
        self.geometry("380x360")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        datos = datos or {}

        etiqueta_formulario(self, "Nombre:", primera=True)
        self.nombre_var = tk.StringVar(value=datos.get("nombre", ""))
        tk.Entry(self, textvariable=self.nombre_var, font=("Arial", 10)).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Teléfono:")
        self.telefono_var = tk.StringVar(value=str(datos.get("telefono") or ""))
        tk.Entry(self, textvariable=self.telefono_var, font=("Arial", 10)).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Estado:")
        self.status_var = tk.StringVar(value=datos.get("status") or STATUS_ASOCIADO_OPCIONES[0])
        ttk.Combobox(
            self, textvariable=self.status_var, state="readonly",
            values=list(STATUS_ASOCIADO_OPCIONES), font=("Arial", 10),
        ).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Notas:")
        self.notas_text = tk.Text(self, font=("Arial", 10), height=4)
        self.notas_text.pack(fill="x", padx=20)
        if datos.get("notas"):
            self.notas_text.insert("1.0", str(datos.get("notas")))

        tk.Button(
            self, text="Guardar", font=("Arial", 11, "bold"), bg=COLOR_MARCA, fg="white",
            relief="flat", padx=15, pady=8, command=self._guardar,
        ).pack(pady=20)

    def _guardar(self) -> None:
        """Valida el nombre y persiste; el error de dominio no escapa (R3, R9).

        Se captura `core.AsociadoError` y no `Exception`: cualquier otro fallo
        es un defecto, no un caso de negocio, y debe propagarse
        (`.langs/python.md` 6).

        Time: O(1) | Space: O(1)
        """
        nombre = self.nombre_var.get().strip()
        telefono = self.telefono_var.get().strip()
        notas = self.notas_text.get("1.0", tk.END).strip()
        status = self.status_var.get()

        if not nombre:
            messagebox.showwarning(APP_TITLE, "El nombre es obligatorio.")
            return

        try:
            if self.modo == "agregar":
                core.crear_asociado(self.app.conn, nombre, telefono, notas, status)
            else:
                core.editar_asociado(
                    self.app.conn, self.asociado_id,
                    nombre=nombre, telefono=telefono, notas=notas, status=status,
                )
        except core.AsociadoError as e:
            logger.exception("Fallo el guardado del asociado (modo %s)", self.modo)
            messagebox.showerror(APP_TITLE, f"No se pudo guardar:\n{e}")
            return

        self.app.refrescar_todo()
        self.destroy()


# ======================================================================
# Pestana: Clientes (directorio de compradores finales)
# ======================================================================

class TabClientes(ttk.Frame):
    """Directorio de clientes finales (CLI-01, R8).

    Espeja `TabAsociados`: barra Agregar/Editar/Eliminar sobre un Treeview cuyo
    `iid` es el `clientes.id` real, de modo que la seleccion sobrevive a
    cualquier reordenamiento del listado.
    """

    def __init__(self, notebook, app) -> None:
        super().__init__(notebook)
        self.app = app
        # Cache del ultimo listado, para resolver la fila seleccionada sin
        # volver a consultar la base (anti N+1).
        self.clientes = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)

        tk.Button(
            barra, text="➕ Agregar cliente", font=("Arial", 10, "bold"),
            bg=COLOR_MARCA, fg="white", relief="flat", padx=12, pady=6,
            command=self._agregar,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            barra, text="✏️ Editar", font=("Arial", 10),
            bg="#EEEEEE", relief="flat", padx=12, pady=6, command=self._editar,
        ).pack(side="left", padx=8)

        tk.Button(
            barra, text="🗑️ Eliminar", font=("Arial", 10),
            bg="#EEEEEE", relief="flat", padx=12, pady=6, command=self._eliminar,
        ).pack(side="left", padx=8)

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("nombre", "telefono", "direccion", "notas")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "nombre": "Nombre", "telefono": "Teléfono",
            "direccion": "Dirección", "notas": "Notas",
        }
        anchos = {"nombre": 200, "telefono": 130, "direccion": 260, "notas": 260}
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="center" if col == "telefono" else "w")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self) -> None:
        """Repuebla la tabla desde `core.listar_clientes` (R1, R8).

        Time: O(n) sobre los clientes | Space: O(n)
        """
        self.tree.delete(*self.tree.get_children())
        self.clientes = core.listar_clientes(self.app.conn)
        for fila in self.clientes:
            self.tree.insert("", "end", iid=str(fila["id"]), values=(
                fila.get("nombre", ""),
                fila.get("telefono", "") or "",
                fila.get("direccion", "") or "",
                fila.get("notas", "") or "",
            ))

    def _id_seleccionado(self) -> int | None:
        """`clientes.id` de la fila seleccionada, o `None` si no hay ninguna.

        Time: O(1) | Space: O(1)
        """
        seleccion = self.tree.selection()
        if not seleccion:
            messagebox.showinfo(APP_TITLE, "Selecciona un cliente de la lista primero.")
            return None
        return int(seleccion[0])

    def _datos(self, cliente_id):
        """Fila cacheada del cliente, o `None` si el listado ya no la tiene.

        Time: O(n) sobre el listado en memoria | Space: O(1)
        """
        return next((f for f in self.clientes if int(f["id"]) == cliente_id), None)

    def _agregar(self) -> None:
        VentanaClienteForm(self.app, modo="agregar")

    def _editar(self) -> None:
        cliente_id = self._id_seleccionado()
        if cliente_id is None:
            return
        datos = self._datos(cliente_id)
        if datos is None:
            return
        VentanaClienteForm(self.app, modo="editar", cliente_id=cliente_id, datos=datos)

    def _eliminar(self) -> None:
        """Da de baja al cliente seleccionado y muestra el motivo si se niega.

        `core.eliminar_cliente` levanta `ClienteError` cuando las FKs de ventas
        o encargos protegen la fila (R6): se registra y se traduce a un
        `messagebox`, la excepcion nunca escapa (R10).

        Time: O(1) | Space: O(1)
        """
        cliente_id = self._id_seleccionado()
        if cliente_id is None:
            return
        if not messagebox.askyesno(APP_TITLE, "¿Eliminar este cliente del directorio?"):
            return

        try:
            core.eliminar_cliente(self.app.conn, cliente_id)
        except core.ClienteError as e:
            logger.exception("Fallo la baja del cliente id=%s", cliente_id)
            messagebox.showerror(APP_TITLE, str(e))
            return

        self.app.refrescar_todo()


# ======================================================================
# Dialogo: agregar/editar cliente
# ======================================================================

class VentanaClienteForm(tk.Toplevel):
    """Alta/edicion de un cliente contra la capa core (CLI-01, R9).

    En modo editar recibe el `clientes.id` real y las claves del dict que
    entrega `core.listar_clientes` (`nombre`, `telefono`, `direccion`, `notas`).
    """

    def __init__(self, app, modo="agregar", cliente_id=None, datos=None) -> None:
        super().__init__(app)
        self.app = app
        self.modo = modo
        self.cliente_id = cliente_id
        self.title("Agregar cliente" if modo == "agregar" else "Editar cliente")
        self.geometry("380x360")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        datos = datos or {}

        etiqueta_formulario(self, "Nombre:", primera=True)
        self.nombre_var = tk.StringVar(value=datos.get("nombre", ""))
        tk.Entry(self, textvariable=self.nombre_var, font=("Arial", 10)).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Teléfono:")
        self.telefono_var = tk.StringVar(value=str(datos.get("telefono") or ""))
        tk.Entry(self, textvariable=self.telefono_var, font=("Arial", 10)).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Dirección:")
        self.direccion_var = tk.StringVar(value=str(datos.get("direccion") or ""))
        tk.Entry(self, textvariable=self.direccion_var, font=("Arial", 10)).pack(fill="x", padx=20)

        etiqueta_formulario(self, "Notas:")
        self.notas_text = tk.Text(self, font=("Arial", 10), height=4)
        self.notas_text.pack(fill="x", padx=20)
        if datos.get("notas"):
            self.notas_text.insert("1.0", str(datos.get("notas")))

        tk.Button(
            self, text="Guardar", font=("Arial", 11, "bold"), bg=COLOR_MARCA, fg="white",
            relief="flat", padx=15, pady=8, command=self._guardar,
        ).pack(pady=20)

    def _guardar(self) -> None:
        """Valida el nombre y persiste; el error de dominio no escapa (R3, R9).

        Un nombre en blanco corta antes de tocar la base: se avisa y no se
        persiste nada. El error de negocio se captura como `core.ClienteError`,
        nunca como `Exception` (`.langs/python.md` 6).

        Time: O(1) | Space: O(1)
        """
        nombre = self.nombre_var.get().strip()
        telefono = self.telefono_var.get().strip()
        direccion = self.direccion_var.get().strip()
        notas = self.notas_text.get("1.0", tk.END).strip()

        if not nombre:
            messagebox.showwarning(APP_TITLE, "El nombre es obligatorio.")
            return

        try:
            if self.modo == "agregar":
                core.crear_cliente(self.app.conn, nombre, telefono, direccion, notas)
            else:
                core.editar_cliente(
                    self.app.conn, self.cliente_id,
                    nombre=nombre, telefono=telefono, direccion=direccion, notas=notas,
                )
        except core.ClienteError as e:
            logger.exception("Fallo el guardado del cliente (modo %s)", self.modo)
            messagebox.showerror(APP_TITLE, f"No se pudo guardar:\n{e}")
            return

        self.app.refrescar_todo()
        self.destroy()


# ======================================================================
# Dialogo: detalle / actualizar entrega a Asociado
# ======================================================================

class VentanaDetalleEntrega(tk.Toplevel):
    def __init__(self, app, entrega):
        super().__init__(app)
        self.app = app
        self.entrega = entrega
        self.title("Entrega a Asociado")
        self.geometry("420x480")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        tk.Label(
            self, text=entrega.get("Descripcion", ""), font=("Arial", 13, "bold"),
            bg="#FFFFFF", fg=COLOR_MARCA, wraplength=380,
        ).pack(pady=(20, 5))
        tk.Label(
            self,
            text=f"Código: {entrega.get('Codigo', '')}   |   Cantidad: {entrega.get('Cantidad entregada', 0)}\n"
                 f"Debe pagar: ${float(entrega.get('Monto que debe pagar', 0) or 0):.2f}",
            font=("Arial", 10), bg="#FFFFFF", fg="#444444",
        ).pack(pady=(0, 15))

        tk.Label(self, text="Status:", bg="#FFFFFF", font=("Arial", 10)).pack(anchor="w", padx=20)
        self.status_var = tk.StringVar(value=entrega.get("Status", core.STATUS_ASOCIADO_OPCIONES[0]))
        ttk.Combobox(
            self, textvariable=self.status_var, state="readonly",
            values=core.STATUS_ASOCIADO_OPCIONES, font=("Arial", 10),
        ).pack(fill="x", padx=20, pady=(0, 15))

        form = tk.Frame(self, bg="#FFFFFF")
        form.pack(fill="x", padx=20)

        tk.Label(form, text="Forma de pago 1:", bg="#FFFFFF", font=("Arial", 10)).grid(row=0, column=0, sticky="w", pady=5)
        self.pago1_var = tk.StringVar(value=entrega.get("Forma de pago 1", "") or "")
        ttk.Combobox(form, textvariable=self.pago1_var, state="readonly", width=15, values=[""] + core.FORMA_PAGO_OPCIONES).grid(row=0, column=1)

        tk.Label(form, text="Monto 1:", bg="#FFFFFF", font=("Arial", 10)).grid(row=1, column=0, sticky="w", pady=5)
        self.monto1_var = tk.StringVar(value=str(entrega.get("Monto 1", "") or ""))
        tk.Entry(form, textvariable=self.monto1_var, width=17).grid(row=1, column=1)

        tk.Label(form, text="Forma de pago 2:", bg="#FFFFFF", font=("Arial", 10)).grid(row=2, column=0, sticky="w", pady=5)
        self.pago2_var = tk.StringVar(value=entrega.get("Forma de pago 2", "") or "")
        ttk.Combobox(form, textvariable=self.pago2_var, state="readonly", width=15, values=[""] + core.FORMA_PAGO_OPCIONES).grid(row=2, column=1)

        tk.Label(form, text="Monto 2:", bg="#FFFFFF", font=("Arial", 10)).grid(row=3, column=0, sticky="w", pady=5)
        self.monto2_var = tk.StringVar(value=str(entrega.get("Monto 2", "") or ""))
        tk.Entry(form, textvariable=self.monto2_var, width=17).grid(row=3, column=1)

        tk.Label(self, text="Observaciones:", bg="#FFFFFF", font=("Arial", 10)).pack(anchor="w", padx=20, pady=(15, 0))
        self.obs_text = tk.Text(self, font=("Arial", 10), height=3)
        self.obs_text.pack(fill="x", padx=20)
        if entrega.get("Observaciones"):
            self.obs_text.insert("1.0", str(entrega.get("Observaciones")))

        tk.Button(
            self, text="Guardar", font=("Arial", 11, "bold"), bg=COLOR_MARCA, fg="white",
            relief="flat", padx=15, pady=8, command=self._guardar,
        ).pack(pady=20)

    def _parsear_monto(self, texto):
        texto = texto.strip()
        if not texto:
            return None
        try:
            return float(texto)
        except ValueError:
            return None

    def _guardar(self):
        monto1 = self._parsear_monto(self.monto1_var.get())
        monto2 = self._parsear_monto(self.monto2_var.get())

        try:
            core.actualizar_entrega_asociado(
                EXCEL_PATH,
                indice=self.entrega["_indice"],
                status=self.status_var.get(),
                forma_pago_1=self.pago1_var.get() or None,
                monto_1=monto1,
                forma_pago_2=self.pago2_var.get() or None,
                monto_2=monto2,
                observaciones=self.obs_text.get("1.0", tk.END).strip(),
            )
        except Exception as e:
            logger.exception("Fallo la actualizacion de la entrega al asociado")
            messagebox.showerror(APP_TITLE, f"No se pudo guardar:\n{e}")
            return

        self.app.refrescar_todo()
        self.destroy()


# ======================================================================
# Ventana: Registrar venta
# ======================================================================

def _etiquetas_cliente(clientes: list[dict]) -> dict[str, int | None]:
    """Mapa `etiqueta del combo -> cliente_id` para la ventana de venta (R14).

    La primera entrada es siempre `CLIENTE_MOSTRADOR`, que significa una venta
    sin cliente registrado (`cliente_id = None`). Dos clientes homonimos no
    pueden compartir etiqueta o la seleccion seria ambigua: al segundo se le
    anexa su id.

    Time: O(n) sobre los clientes | Space: O(n)
    """
    etiquetas: dict[str, int | None] = {core.CLIENTE_MOSTRADOR: None}
    for cliente in clientes:
        cliente_id = int(cliente["id"])
        etiqueta = str(cliente.get("nombre", "")).strip() or f"Cliente #{cliente_id}"
        if etiqueta in etiquetas:
            etiqueta = f"{etiqueta} (#{cliente_id})"
        etiquetas[etiqueta] = cliente_id
    return etiquetas


class VentanaVenta(tk.Toplevel):
    """Registro de una venta como **canasta** multi-producto (CLI-02, R14).

    El flujo es: buscar un producto, capturar cantidad y precio publico,
    agregarlo a la canasta, repetir, elegir el cliente (o dejar `Mostrador`) y
    registrar. La venta entera viaja en una sola llamada a
    `core.registrar_venta`, que la escribe de forma atomica: o entran todas las
    lineas o no entra ninguna.

    El stock **no** se juzga aqui. La ventana solo valida la forma de lo que se
    captura (cantidad entera positiva, precio numerico); si falta inventario lo
    dice el core con el disponible real, y su mensaje se muestra inline dejando
    la canasta intacta para que la usuaria la corrija.

    Los pagos no son de esta ventana: `venta_pagos` es dominio de CLI-03.
    """

    def __init__(self, app, codigo_preseleccionado=None):
        super().__init__(app)
        self.app = app
        self.title("Registrar venta")
        self.geometry("620x760")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        self.catalogo = core.obtener_existencias(self.app.conn)
        self.clientes_por_etiqueta = _etiquetas_cliente(core.listar_clientes(self.app.conn))
        self.producto_seleccionado = None
        # Lineas de la canasta indexadas por el `iid` de su fila en el arbol,
        # que es lo unico estable cuando se quita una linea intermedia.
        self.lineas_canasta: dict[str, dict] = {}
        self._contador_linea = 0

        self._construir_interfaz()
        self._filtrar_lista()

        if codigo_preseleccionado:
            for i, prod in enumerate(self.catalogo):
                if str(prod["Codigo articulo"]) == str(codigo_preseleccionado):
                    self.busqueda_var.set(str(codigo_preseleccionado))
                    break

    def _construir_interfaz(self):
        """Monta las tres zonas de la ventana: buscador, captura y canasta."""
        tk.Label(
            self, text="Registrar venta", font=("Arial", 15, "bold"),
            bg="#FFFFFF", fg=COLOR_MARCA,
        ).pack(pady=(15, 10))

        self._construir_buscador()
        self._construir_formulario()
        self._construir_canasta()

        tk.Button(
            self, text="✅  Registrar venta", font=("Arial", 12, "bold"),
            bg=COLOR_ROSA, fg="white", activebackground="#b8005f",
            relief="flat", padx=15, pady=10, command=self._registrar,
        ).pack(pady=(10, 8))

        self.status_label = tk.Label(
            self, text="", font=("Arial", 9), bg="#FFFFFF", fg="#CC0000",
            wraplength=560, justify="left",
        )
        self.status_label.pack(fill="x", padx=20, pady=(0, 10))

    def _construir_buscador(self):
        """Buscador + lista del catalogo con el detalle del producto elegido."""
        tk.Label(
            self, text="Busca por código o nombre:", font=("Arial", 10),
            bg="#FFFFFF", anchor="w",
        ).pack(fill="x", padx=20)

        self.busqueda_var = tk.StringVar()
        self.busqueda_var.trace_add("write", lambda *_: self._filtrar_lista())
        entry_busqueda = tk.Entry(self, textvariable=self.busqueda_var, font=("Arial", 11))
        entry_busqueda.pack(fill="x", padx=20, pady=(0, 10))
        entry_busqueda.focus_set()

        frame_lista = tk.Frame(self, bg="#FFFFFF")
        frame_lista.pack(fill="both", padx=20)
        scrollbar = tk.Scrollbar(frame_lista)
        scrollbar.pack(side="right", fill="y")
        self.listbox = tk.Listbox(
            frame_lista, height=7, font=("Arial", 10),
            yscrollcommand=scrollbar.set, exportselection=False,
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._al_seleccionar)

        self.info_label = tk.Label(
            self, text="Selecciona un producto de la lista.",
            font=("Arial", 9), bg="#F5F5F5", fg="#444444",
            justify="left", anchor="w", wraplength=560,
        )
        self.info_label.pack(fill="x", padx=20, pady=(10, 10))

    def _construir_formulario(self):
        """Cantidad, precio, cliente y observaciones, con el boton de agregar."""
        form = tk.Frame(self, bg="#FFFFFF")
        form.pack(fill="x", padx=20)

        tk.Label(form, text="Cantidad vendida:", bg="#FFFFFF", font=("Arial", 10)).grid(row=0, column=0, sticky="w", pady=4)
        self.cantidad_var = tk.StringVar()
        tk.Entry(form, textvariable=self.cantidad_var, font=("Arial", 10), width=12).grid(row=0, column=1, sticky="w")

        tk.Label(form, text="Precio público ($):", bg="#FFFFFF", font=("Arial", 10)).grid(row=1, column=0, sticky="w", pady=4)
        self.precio_var = tk.StringVar()
        tk.Entry(form, textvariable=self.precio_var, font=("Arial", 10), width=12).grid(row=1, column=1, sticky="w")

        tk.Button(
            form, text="➕ Agregar a la canasta", font=("Arial", 10, "bold"),
            bg=COLOR_MARCA, fg="white", relief="flat", padx=10, pady=4,
            command=self._agregar_linea,
        ).grid(row=0, column=2, rowspan=2, padx=15)

        tk.Label(form, text="Cliente:", bg="#FFFFFF", font=("Arial", 10)).grid(row=2, column=0, sticky="w", pady=4)
        self.cliente_var = tk.StringVar(value=core.CLIENTE_MOSTRADOR)
        self.combo_cliente = ttk.Combobox(
            form, textvariable=self.cliente_var, state="readonly", width=28,
            values=list(self.clientes_por_etiqueta),
        )
        self.combo_cliente.grid(row=2, column=1, columnspan=2, sticky="w")

        tk.Label(form, text="Observaciones:", bg="#FFFFFF", font=("Arial", 10)).grid(row=3, column=0, sticky="nw", pady=4)
        self.obs_text = tk.Text(form, font=("Arial", 10), width=34, height=2)
        self.obs_text.grid(row=3, column=1, columnspan=2, sticky="w")

    def _construir_canasta(self):
        """Tabla de la canasta y el boton que quita la linea seleccionada."""
        cabecera = tk.Frame(self, bg="#FFFFFF")
        cabecera.pack(fill="x", padx=20, pady=(12, 4))
        tk.Label(cabecera, text="Canasta de la venta:", bg="#FFFFFF", font=("Arial", 10, "bold")).pack(side="left")
        tk.Button(
            cabecera, text="🗑️ Quitar línea", font=("Arial", 9),
            bg="#EEEEEE", relief="flat", padx=10, pady=3, command=self._quitar_linea,
        ).pack(side="right")

        marco = tk.Frame(self, bg="#FFFFFF")
        marco.pack(fill="both", padx=20)

        columnas = ("codigo", "descripcion", "cantidad", "precio", "importe")
        self.tree_canasta = ttk.Treeview(marco, columns=columnas, show="headings", height=6)
        titulos = {
            "codigo": "Código", "descripcion": "Descripción", "cantidad": "Cant.",
            "precio": "Precio público", "importe": "Importe",
        }
        anchos = {"codigo": 70, "descripcion": 240, "cantidad": 55, "precio": 100, "importe": 90}
        for col in columnas:
            self.tree_canasta.heading(col, text=titulos[col])
            self.tree_canasta.column(col, width=anchos[col], anchor="w" if col == "descripcion" else "center")
        self.tree_canasta.pack(fill="both", expand=True)

    def _filtrar_lista(self):
        texto = self.busqueda_var.get().strip().lower()
        self.listbox.delete(0, tk.END)
        self._indices_filtrados = []
        for i, prod in enumerate(self.catalogo):
            codigo = str(prod["Codigo articulo"]).lower()
            desc = str(prod["Descripcion"]).lower()
            if texto and texto not in codigo and texto not in desc:
                continue
            etiqueta = f'{prod["Codigo articulo"]} - {prod["Descripcion"]}  (disponibles: {int(prod["Piezas disponibles"])})'
            self.listbox.insert(tk.END, etiqueta)
            self._indices_filtrados.append(i)

        if len(self._indices_filtrados) == 1:
            self.listbox.selection_set(0)
            self._al_seleccionar(None)

    def _al_seleccionar(self, event):
        seleccion = self.listbox.curselection()
        if not seleccion:
            return
        idx_real = self._indices_filtrados[seleccion[0]]
        self.producto_seleccionado = self.catalogo[idx_real]
        p = self.producto_seleccionado
        self.info_label.config(
            text=(
                f"Producto: {p['Descripcion']} (código {p['Codigo articulo']})\n"
                f"Piezas disponibles: {int(p['Piezas disponibles'])}\n"
                f"Tu costo aproximado por pieza: ${p['Precio unitario costo']:.2f}"
            ),
            fg="#444444",
        )
        self.status_label.config(text="")

    # ------------------------------------------------------------------
    # Canasta
    # ------------------------------------------------------------------

    def _cantidad_capturada(self) -> int | None:
        """Cantidad del formulario, o `None` avisando inline si no sirve.

        Time: O(n) sobre el largo del texto | Space: O(1)
        """
        try:
            cantidad = int(self.cantidad_var.get())
        except ValueError:
            self.status_label.config(text="La cantidad vendida debe ser un número entero.")
            return None
        if cantidad <= 0:
            self.status_label.config(text="La cantidad vendida debe ser mayor que cero.")
            return None
        return cantidad

    def _precio_capturado(self) -> float | None:
        """Precio publico del formulario, o `None` avisando inline si no sirve.

        Time: O(n) sobre el largo del texto | Space: O(1)
        """
        try:
            precio = float(self.precio_var.get())
        except ValueError:
            self.status_label.config(text="El precio público debe ser un número (ej. 150 o 150.50).")
            return None
        if precio < 0:
            self.status_label.config(text="El precio público no puede ser negativo.")
            return None
        return precio

    def _agregar_linea(self) -> None:
        """Empuja el producto seleccionado a la canasta (R14).

        Aqui solo se valida la forma de lo capturado; el stock lo sigue
        juzgando `core.registrar_venta` al final, que es quien ve la base.

        Time: O(1) | Space: O(1)
        """
        self.status_label.config(text="")
        if not self.producto_seleccionado:
            self.status_label.config(text="Primero selecciona un producto de la lista.")
            return

        cantidad = self._cantidad_capturada()
        if cantidad is None:
            return
        precio_publico = self._precio_capturado()
        if precio_publico is None:
            return

        producto = self.producto_seleccionado
        self._insertar_linea(
            {
                "codigo": str(producto["Codigo articulo"]),
                "cantidad": cantidad,
                "precio_publico": precio_publico,
            },
            str(producto["Descripcion"]),
        )
        self.cantidad_var.set("")
        self.precio_var.set("")

    def _insertar_linea(self, linea: dict, descripcion: str) -> None:
        """Agrega `linea` a la canasta con un `iid` propio e irrepetible.

        El `iid` no puede ser la posicion de la fila: al quitar una linea
        intermedia las posteriores se recorrerian y la seleccion pasaria a
        apuntar a otra linea.

        Time: O(1) | Space: O(1)
        """
        self._contador_linea += 1
        iid = f"L{self._contador_linea}"
        self.lineas_canasta[iid] = linea
        importe = linea["cantidad"] * linea["precio_publico"]
        self.tree_canasta.insert("", "end", iid=iid, values=(
            linea["codigo"], descripcion, linea["cantidad"],
            f"${linea['precio_publico']:.2f}", f"${importe:.2f}",
        ))

    def _quitar_linea(self) -> None:
        """Quita de la canasta la(s) linea(s) seleccionada(s) (R14).

        Time: O(k) sobre la seleccion | Space: O(1)
        """
        self.status_label.config(text="")
        seleccion = self.tree_canasta.selection()
        if not seleccion:
            self.status_label.config(text="Selecciona una línea de la canasta para quitarla.")
            return
        for iid in seleccion:
            self.lineas_canasta.pop(iid, None)
            self.tree_canasta.delete(iid)

    def _canasta(self) -> list[dict]:
        """Lineas de la canasta en el orden en que se capturaron.

        Time: O(n) | Space: O(n)
        """
        return [self.lineas_canasta[iid] for iid in self.tree_canasta.get_children()]

    def _limpiar_canasta(self) -> None:
        """Vacia la canasta y las observaciones tras una venta registrada.

        Time: O(n) | Space: O(1)
        """
        self.tree_canasta.delete(*self.tree_canasta.get_children())
        self.lineas_canasta.clear()
        self.obs_text.delete("1.0", tk.END)

    def _registrar(self) -> None:
        """Registra la canasta completa contra la capa core (R14).

        `core.registrar_venta` recibe la conexion de la sesion y escribe las
        lineas de forma atomica. Un `VentaError` --stock insuficiente, codigo
        inexistente, cliente inexistente-- se muestra **inline**, porque trae el
        disponible real y la canasta se deja intacta para poder corregirla; solo
        un fallo de dominio inesperado interrumpe con un dialogo. Nunca se
        captura `Exception` (`.langs/python.md` 6).

        Time: O(n) sobre las lineas de la canasta | Space: O(n)
        """
        self.status_label.config(text="")
        lineas = self._canasta()
        if not lineas:
            self.status_label.config(text="La canasta está vacía: agrega al menos un producto.")
            return

        cliente_id = self.clientes_por_etiqueta.get(self.cliente_var.get())
        observaciones = self.obs_text.get("1.0", tk.END).strip()

        try:
            resultado = core.registrar_venta(self.app.conn, cliente_id, lineas, observaciones)
        except core.VentaError as e:
            logger.exception("Fallo el registro de la venta (%d linea(s))", len(lineas))
            self.status_label.config(text=str(e))
            return
        except core.CoreError as e:
            logger.exception("Fallo inesperado de dominio al registrar la venta")
            messagebox.showerror("Registrar venta", f"Ocurrió un error inesperado:\n{e}")
            return

        messagebox.showinfo(
            "Venta registrada",
            f"Venta #{resultado['venta_id']} registrada con "
            f"{resultado['num_lineas']} línea(s).\n"
            f"Total: ${resultado['total']:.2f}\n"
            f"Ganancia: ${resultado['ganancia']:.2f}",
        )

        self.app.refrescar_todo()

        self._limpiar_canasta()
        self.catalogo = core.obtener_existencias(self.app.conn)
        self.producto_seleccionado = None
        self.cantidad_var.set("")
        self.precio_var.set("")
        self.info_label.config(text="Selecciona un producto de la lista.")
        self._filtrar_lista()


# ======================================================================
# Ventana: Vista previa antes de guardar (carga de PDFs)
# ======================================================================

class VentanaPrevisualizacion(tk.Toplevel):
    """Muestra los productos leidos del/los PDF antes de guardarlos,
    para poder corregir cantidades o precios (por ejemplo, productos
    de promocion o de regalo que no traen el precio real), y para
    repartir donde quedo cada pieza: con el Asociado, en Casa o en el
    Local. Lo que se lleva el Asociado NO cuenta como tu stock."""

    COLUMNAS_ENTERAS = {"Cantidad surtida", "Cantidad Asociado", "Cantidad Casa", "Cantidad Local"}
    COLUMNAS_DINERO = {"Precio catalogo", "Precio con IVA", "Precio que pagas"}
    COLUMNAS_UBICACION = {"Cantidad Asociado", "Cantidad Casa", "Cantidad Local"}

    COLUMNAS = [
        ("Codigo articulo", "Código", 65, False),
        ("Descripcion", "Descripción", 170, False),
        ("Tipo", "Tipo", 115, False),
        ("Nombre asociado", "Asociado", 130, False),
        ("Cantidad surtida", "Cant. total", 65, True),
        ("Cantidad Asociado", "Asociado", 65, True),
        ("Cantidad Casa", "Casa", 55, True),
        ("Cantidad Local", "Local", 55, True),
        ("Precio catalogo", "Precio catálogo", 90, True),
        ("Precio con IVA", "Precio con IVA", 90, True),
        ("Precio que pagas", "Precio que pagas (tu costo)", 140, True),
    ]

    def __init__(self, app, filas, errores):
        super().__init__(app)
        self.app = app
        # Copia editable con el reparto ya normalizado: todo al asociado de la
        # nota (lo normal), o todo a Casa y marcado para revision cuando la
        # nota no trae asociado. La copia no filtra llaves: `asociado_id` y las
        # marcas viajan intactas hasta `_confirmar`.
        self.filas = core.normalizar_reparto_carga([dict(f) for f in filas])
        self.title("Vista previa antes de guardar")
        self.geometry("970x500")
        self.configure(bg="#FFFFFF")

        tk.Label(
            self, text="Revisa lo que se va a cargar", font=("Arial", 14, "bold"),
            bg="#FFFFFF", fg=COLOR_MARCA,
        ).pack(pady=(15, 2))
        tk.Label(
            self,
            text="Doble clic sobre cualquier columna numérica para corregirla. "
                 "\"Asociado\" + \"Casa\" + \"Local\" debe sumar la Cant. total.\n"
                 "Lo que se lleva el Asociado no cuenta como tu inventario; Casa y Local sí.",
            font=("Arial", 9), bg="#FFFFFF", fg="#666666", justify="center",
        ).pack(pady=(0, 10))

        if errores:
            tk.Label(
                self, text="Avisos:\n" + "\n".join(errores), font=("Arial", 9),
                bg="#FFF7E6", fg="#996600", justify="left", anchor="w", wraplength=900,
            ).pack(fill="x", padx=15, pady=(0, 8))

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15)

        columnas_ids = [c[0] for c in self.COLUMNAS]
        self.tree = ttk.Treeview(frame_tabla, columns=columnas_ids, show="headings", height=14)
        for col_id, titulo, ancho, _editable in self.COLUMNAS:
            self.tree.heading(col_id, text=titulo)
            self.tree.column(col_id, width=ancho, anchor="center" if col_id != "Descripcion" else "w")

        # Filas cuya nota no trae asociado: se resaltan para que se revisen
        # antes de confirmar (su cantidad quedo en Casa, no con el Asociado).
        self.tree.tag_configure("revisar", background="#FFF7E6", foreground="#996600")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._llenar_tabla()
        self.tree.bind("<Double-1>", self._editar_celda)

        botones = tk.Frame(self, bg="#FFFFFF")
        botones.pack(pady=15)

        tk.Button(
            botones, text="✅  Confirmar y cargar al inventario", font=("Arial", 11, "bold"),
            bg=COLOR_ROSA, fg="white", relief="flat", padx=15, pady=8,
            command=self._confirmar,
        ).pack(side="left", padx=8)

        tk.Button(
            botones, text="Cancelar", font=("Arial", 10),
            bg="#EEEEEE", fg="#333333", relief="flat", padx=15, pady=8,
            command=self.destroy,
        ).pack(side="left", padx=8)

    def _formatear_valor(self, col_id, valor):
        if col_id in self.COLUMNAS_DINERO:
            try:
                return f"{float(valor):.2f}"
            except (TypeError, ValueError):
                return "0.00"
        if col_id in self.COLUMNAS_ENTERAS:
            try:
                return int(valor)
            except (TypeError, ValueError):
                return 0
        return valor

    def _llenar_tabla(self):
        self.tree.delete(*self.tree.get_children())
        for i, fila in enumerate(self.filas):
            valores = [self._formatear_valor(col_id, fila.get(col_id, "")) for col_id, *_ in self.COLUMNAS]
            etiquetas = ("revisar",) if fila.get("_revisar_asociado") else ()
            self.tree.insert("", "end", iid=str(i), values=valores, tags=etiquetas)

    def _editar_celda(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        fila_id = self.tree.identify_row(event.y)
        columna_id = self.tree.identify_column(event.x)  # ej. '#4'
        if not fila_id or not columna_id:
            return
        indice_col = int(columna_id.replace("#", "")) - 1
        col_nombre, _titulo, _ancho, editable = self.COLUMNAS[indice_col]
        if not editable:
            return

        x, y, ancho, alto = self.tree.bbox(fila_id, columna_id)
        valor_actual = self.tree.set(fila_id, col_nombre)

        entry = tk.Entry(self.tree, font=("Arial", 10))
        entry.place(x=x, y=y, width=ancho, height=alto)
        entry.insert(0, valor_actual)
        entry.focus_set()
        entry.select_range(0, tk.END)

        def guardar_edicion(_event=None):
            nuevo_valor = entry.get().strip()
            entry.destroy()
            indice_fila = int(fila_id)

            if col_nombre in self.COLUMNAS_ENTERAS:
                try:
                    nuevo_valor_num = int(float(nuevo_valor))
                except ValueError:
                    return
            else:
                try:
                    nuevo_valor_num = round(float(nuevo_valor), 2)
                except ValueError:
                    return

            fila_actual = self.filas[indice_fila]
            total = int(fila_actual.get("Cantidad surtida", 0) or 0)

            if col_nombre in self.COLUMNAS_UBICACION:
                if nuevo_valor_num < 0:
                    messagebox.showwarning("Cantidad inválida", "La cantidad no puede ser negativa.")
                    return
                otras_columnas = self.COLUMNAS_UBICACION - {col_nombre}
                suma_otras = sum(int(fila_actual.get(c, 0) or 0) for c in otras_columnas)
                if suma_otras + nuevo_valor_num > total:
                    disponible = max(total - suma_otras, 0)
                    messagebox.showwarning(
                        "Cantidad excedida",
                        f"'{fila_actual['Descripcion']}': solo llegaron {total} pieza(s) en total.\n"
                        f"Ya repartiste {suma_otras} en las otras columnas, así que aquí puedes "
                        f"poner como máximo {disponible}.",
                    )
                    return
            elif col_nombre == "Cantidad surtida":
                suma_ubicacion = sum(int(fila_actual.get(c, 0) or 0) for c in self.COLUMNAS_UBICACION)
                if nuevo_valor_num < suma_ubicacion:
                    messagebox.showwarning(
                        "Cantidad inválida",
                        f"'{fila_actual['Descripcion']}': ya repartiste {suma_ubicacion} pieza(s) entre "
                        f"Asociado/Casa/Local, así que la cantidad total no puede ser menor a eso.",
                    )
                    return

            self.filas[indice_fila][col_nombre] = nuevo_valor_num

            # Mantener consistente el Valor total con IVA con la
            # cantidad total y el precio con IVA (unitario).
            cantidad = self.filas[indice_fila].get("Cantidad surtida", 0)
            precio_con_iva = self.filas[indice_fila].get("Precio con IVA", 0)
            try:
                self.filas[indice_fila]["Valor total con IVA"] = round(float(cantidad) * float(precio_con_iva), 2)
            except (TypeError, ValueError):
                pass

            self.tree.set(fila_id, col_nombre, self._formatear_valor(col_nombre, nuevo_valor_num))

        entry.bind("<Return>", guardar_edicion)
        entry.bind("<FocusOut>", guardar_edicion)

    def _confirmar(self):
        errores_reparto = []
        for fila in self.filas:
            total = int(fila.get("Cantidad surtida", 0) or 0)
            asociado = int(fila.get("Cantidad Asociado", 0) or 0)
            casa = int(fila.get("Cantidad Casa", 0) or 0)
            local = int(fila.get("Cantidad Local", 0) or 0)
            suma = asociado + casa + local
            if suma != total:
                errores_reparto.append(
                    f"{fila['Descripcion']}: repartiste {suma} pieza(s) pero llegaron {total}."
                )

        if errores_reparto:
            messagebox.showerror(
                "Revisa la repartición",
                "Asociado + Casa + Local debe sumar la misma cantidad que llegó. "
                "Corrige estos productos antes de continuar:\n\n" + "\n".join(errores_reparto),
            )
            return

        self.app.al_confirmar_carga(self.filas)
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
