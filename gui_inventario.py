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

import backup
import core
import db

logger = logging.getLogger(__name__)

APP_TITLE = "Inventario Betterware"
COLOR_MARCA = "#12C1B4"
COLOR_ROSA = "#E00176"
COLOR_AZUL = "#005EB8"


def ruta_base():
    """Carpeta donde vive el programa (funciona igual como .py o como .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


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

        self.notebook.add(self.tab_dashboard, text="  📊 Dashboard  ")
        self.notebook.add(self.tab_inventario, text="  📦 Inventario  ")
        self.notebook.add(self.tab_pedidos, text="  🧾 Pedidos  ")
        self.notebook.add(self.tab_ventas, text="  🛒 Ventas  ")
        self.notebook.add(self.tab_entregas, text="  🤝 Entregas Asociado  ")
        self.notebook.add(self.tab_asociados, text="  👥 Asociados  ")

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
            filas, errores = core.preparar_filas_desde_pdfs(list(archivos))
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
        if not os.path.exists(EXCEL_PATH):
            messagebox.showinfo(
                APP_TITLE,
                "Todavía no existe inventario. Primero carga al menos un PDF.",
            )
            return
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

    def refrescar(self):
        if not os.path.exists(EXCEL_PATH):
            self.datos_completos = []
        else:
            self.datos_completos = core.obtener_movimientos(EXCEL_PATH)

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

class TabVentas(ttk.Frame):
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self.datos_completos = []

        barra = tk.Frame(self)
        barra.pack(fill="x", padx=15, pady=15)
        tk.Label(barra, text="Producto/código:", font=("Arial", 10)).pack(side="left")
        self.filtro_var = tk.StringVar()
        self.filtro_var.trace_add("write", lambda *_: self._aplicar_filtro())
        tk.Entry(barra, textvariable=self.filtro_var, font=("Arial", 10), width=25).pack(side="left", padx=8)

        tk.Button(
            barra, text="🛒 Registrar venta", font=("Arial", 10, "bold"),
            bg=COLOR_ROSA, fg="white", relief="flat", padx=12, pady=6,
            command=self.app.abrir_ventana_venta,
        ).pack(side="right")

        frame_tabla = tk.Frame(self)
        frame_tabla.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        columnas = ("fecha", "codigo", "descripcion", "cantidad", "costo", "publico", "total", "ganancia", "pago")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        titulos = {
            "fecha": "Fecha", "codigo": "Código", "descripcion": "Descripción", "cantidad": "Cant.",
            "costo": "Costo", "publico": "Precio público", "total": "Total", "ganancia": "Ganancia", "pago": "Forma de pago",
        }
        anchos = {"fecha": 110, "codigo": 65, "descripcion": 200, "cantidad": 50, "costo": 80, "publico": 90, "total": 80, "ganancia": 80, "pago": 100}
        for col in columnas:
            self.tree.heading(col, text=titulos[col])
            self.tree.column(col, width=anchos[col], anchor="center" if col != "descripcion" else "w")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self):
        if not os.path.exists(EXCEL_PATH):
            self.datos_completos = []
        else:
            self.datos_completos = core.obtener_ventas_historial(EXCEL_PATH)
        self._aplicar_filtro()

    def _aplicar_filtro(self):
        texto = self.filtro_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for venta in self.datos_completos:
            if texto and texto not in str(venta.get("Codigo", "")).lower() and texto not in str(venta.get("Descripcion", "")).lower():
                continue
            self.tree.insert("", "end", values=(
                venta.get("Fecha", ""), venta.get("Codigo", ""), venta.get("Descripcion", ""),
                venta.get("Cantidad vendida", 0), f"${float(venta.get('Precio asociado', 0) or 0):.2f}",
                f"${float(venta.get('Precio publico', 0) or 0):.2f}", f"${float(venta.get('Total', 0) or 0):.2f}",
                f"${float(venta.get('Ganancia', 0) or 0):.2f}", venta.get("Forma de pago", ""),
            ))


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
    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app

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

        columnas = ("nombre", "telefono", "notas")
        self.tree = ttk.Treeview(frame_tabla, columns=columnas, show="headings", height=18)
        self.tree.heading("nombre", text="Nombre")
        self.tree.heading("telefono", text="Teléfono")
        self.tree.heading("notas", text="Notas")
        self.tree.column("nombre", width=200, anchor="w")
        self.tree.column("telefono", width=140, anchor="center")
        self.tree.column("notas", width=350, anchor="w")

        scrollbar = ttk.Scrollbar(frame_tabla, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refrescar(self):
        self.tree.delete(*self.tree.get_children())
        if not os.path.exists(EXCEL_PATH):
            return
        df = core.leer_directorio_asociados(EXCEL_PATH)
        for i, fila in df.iterrows():
            self.tree.insert("", "end", iid=str(i), values=(
                fila.get("Nombre", ""), fila.get("Telefono", ""), fila.get("Notas", "") or "",
            ))

    def _indice_seleccionado(self):
        seleccion = self.tree.selection()
        if not seleccion:
            messagebox.showinfo(APP_TITLE, "Selecciona un asociado de la lista primero.")
            return None
        return int(seleccion[0])

    def _agregar(self):
        VentanaAsociadoForm(self.app, modo="agregar")

    def _editar(self):
        indice = self._indice_seleccionado()
        if indice is None:
            return
        df = core.leer_directorio_asociados(EXCEL_PATH)
        fila = df.loc[indice]
        VentanaAsociadoForm(self.app, modo="editar", indice=indice, datos=fila.to_dict())

    def _eliminar(self):
        indice = self._indice_seleccionado()
        if indice is None:
            return
        if not messagebox.askyesno(APP_TITLE, "¿Eliminar este asociado del directorio?"):
            return
        core.eliminar_asociado(EXCEL_PATH, indice)
        self.app.refrescar_todo()

    def _enviar_whatsapp(self):
        indice = self._indice_seleccionado()
        if indice is None:
            return
        df = core.leer_directorio_asociados(EXCEL_PATH)
        fila = df.loc[indice]
        telefono = fila.get("Telefono", "")
        if not telefono or not str(telefono).strip():
            messagebox.showwarning(APP_TITLE, "Este asociado no tiene teléfono registrado.")
            return
        mensaje = simpledialog.askstring(
            "Mensaje de WhatsApp",
            f"Mensaje para {fila.get('Nombre', '')} (opcional):",
        )
        link = core.link_whatsapp(telefono, mensaje or "")
        webbrowser.open(link)


# ======================================================================
# Dialogo: agregar/editar asociado
# ======================================================================

class VentanaAsociadoForm(tk.Toplevel):
    def __init__(self, app, modo="agregar", indice=None, datos=None):
        super().__init__(app)
        self.app = app
        self.modo = modo
        self.indice = indice
        self.title("Agregar asociado" if modo == "agregar" else "Editar asociado")
        self.geometry("380x300")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        datos = datos or {}

        tk.Label(self, text="Nombre:", bg="#FFFFFF", font=("Arial", 10)).pack(anchor="w", padx=20, pady=(20, 0))
        self.nombre_var = tk.StringVar(value=datos.get("Nombre", ""))
        tk.Entry(self, textvariable=self.nombre_var, font=("Arial", 10)).pack(fill="x", padx=20)

        tk.Label(self, text="Teléfono:", bg="#FFFFFF", font=("Arial", 10)).pack(anchor="w", padx=20, pady=(15, 0))
        self.telefono_var = tk.StringVar(value=str(datos.get("Telefono", "")) if datos.get("Telefono") else "")
        tk.Entry(self, textvariable=self.telefono_var, font=("Arial", 10)).pack(fill="x", padx=20)

        tk.Label(self, text="Notas:", bg="#FFFFFF", font=("Arial", 10)).pack(anchor="w", padx=20, pady=(15, 0))
        self.notas_text = tk.Text(self, font=("Arial", 10), height=4)
        self.notas_text.pack(fill="x", padx=20)
        if datos.get("Notas"):
            self.notas_text.insert("1.0", str(datos.get("Notas")))

        tk.Button(
            self, text="Guardar", font=("Arial", 11, "bold"), bg=COLOR_MARCA, fg="white",
            relief="flat", padx=15, pady=8, command=self._guardar,
        ).pack(pady=20)

    def _guardar(self):
        nombre = self.nombre_var.get().strip()
        telefono = self.telefono_var.get().strip()
        notas = self.notas_text.get("1.0", tk.END).strip()

        if not nombre:
            messagebox.showwarning(APP_TITLE, "El nombre es obligatorio.")
            return

        try:
            if self.modo == "agregar":
                core.agregar_asociado(EXCEL_PATH, nombre, telefono, notas)
            else:
                core.editar_asociado(EXCEL_PATH, self.indice, nombre=nombre, telefono=telefono, notas=notas)
        except Exception as e:
            logger.exception("Fallo el guardado del asociado (modo %s)", self.modo)
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

class VentanaVenta(tk.Toplevel):
    """Ventana para registrar una venta: buscar producto (por código o
    nombre), capturar cantidad y precio, y guardar. Si no hay
    inventario suficiente, no deja registrar la venta."""

    def __init__(self, app, codigo_preseleccionado=None):
        super().__init__(app)
        self.app = app
        self.title("Registrar venta")
        self.geometry("480x580")
        self.resizable(False, False)
        self.configure(bg="#FFFFFF")

        self.catalogo = core.obtener_existencias(self.app.conn)
        self.producto_seleccionado = None

        self._construir_interfaz()
        self._filtrar_lista()

        if codigo_preseleccionado:
            for i, prod in enumerate(self.catalogo):
                if str(prod["Codigo articulo"]) == str(codigo_preseleccionado):
                    self.busqueda_var.set(str(codigo_preseleccionado))
                    break

    def _construir_interfaz(self):
        tk.Label(
            self, text="Registrar venta", font=("Arial", 15, "bold"),
            bg="#FFFFFF", fg=COLOR_MARCA,
        ).pack(pady=(15, 10))

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
            frame_lista, height=8, font=("Arial", 10),
            yscrollcommand=scrollbar.set, exportselection=False,
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)
        self.listbox.bind("<<ListboxSelect>>", self._al_seleccionar)

        self.info_label = tk.Label(
            self, text="Selecciona un producto de la lista.",
            font=("Arial", 9), bg="#F5F5F5", fg="#444444",
            justify="left", anchor="w", wraplength=440,
        )
        self.info_label.pack(fill="x", padx=20, pady=(10, 15))

        form = tk.Frame(self, bg="#FFFFFF")
        form.pack(fill="x", padx=20)

        tk.Label(form, text="Cantidad vendida:", bg="#FFFFFF", font=("Arial", 10)).grid(row=0, column=0, sticky="w", pady=5)
        self.cantidad_var = tk.StringVar()
        tk.Entry(form, textvariable=self.cantidad_var, font=("Arial", 10), width=12).grid(row=0, column=1, sticky="w")

        tk.Label(form, text="Precio público ($):", bg="#FFFFFF", font=("Arial", 10)).grid(row=1, column=0, sticky="w", pady=5)
        self.precio_var = tk.StringVar()
        tk.Entry(form, textvariable=self.precio_var, font=("Arial", 10), width=12).grid(row=1, column=1, sticky="w")

        tk.Label(form, text="Forma de pago:", bg="#FFFFFF", font=("Arial", 10)).grid(row=2, column=0, sticky="w", pady=5)
        self.pago_var = tk.StringVar(value="Efectivo")
        combo_pago = ttk.Combobox(
            form, textvariable=self.pago_var, state="readonly", width=15,
            values=core.FORMA_PAGO_OPCIONES,
        )
        combo_pago.grid(row=2, column=1, sticky="w")

        tk.Label(form, text="Observaciones:", bg="#FFFFFF", font=("Arial", 10)).grid(row=3, column=0, sticky="nw", pady=5)
        self.obs_text = tk.Text(form, font=("Arial", 10), width=28, height=3)
        self.obs_text.grid(row=3, column=1, sticky="w")

        btn_registrar = tk.Button(
            self, text="✅  Registrar venta", font=("Arial", 12, "bold"),
            bg=COLOR_ROSA, fg="white", activebackground="#b8005f",
            relief="flat", padx=15, pady=10, command=self._registrar,
        )
        btn_registrar.pack(pady=20)

        self.status_label = tk.Label(
            self, text="", font=("Arial", 9), bg="#FFFFFF", fg="#CC0000",
            wraplength=440, justify="left",
        )
        self.status_label.pack(fill="x", padx=20)

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

    def _registrar(self):
        self.status_label.config(text="")
        if not self.producto_seleccionado:
            self.status_label.config(text="Primero selecciona un producto de la lista.")
            return

        try:
            cantidad = int(self.cantidad_var.get())
        except ValueError:
            self.status_label.config(text="La cantidad vendida debe ser un número entero.")
            return

        try:
            precio_publico = float(self.precio_var.get())
        except ValueError:
            self.status_label.config(text="El precio público debe ser un número (ej. 150 o 150.50).")
            return

        observaciones = self.obs_text.get("1.0", tk.END).strip()

        try:
            resultado = core.registrar_venta(
                EXCEL_PATH,
                codigo=self.producto_seleccionado["Codigo articulo"],
                cantidad=cantidad,
                precio_publico=precio_publico,
                forma_pago=self.pago_var.get(),
                observaciones=observaciones,
            )
        except core.VentaError as e:
            self.status_label.config(text=str(e))
            return
        except Exception as e:
            logger.exception("Fallo el registro de la venta")
            messagebox.showerror("Registrar venta", f"Ocurrió un error inesperado:\n{e}")
            return

        messagebox.showinfo(
            "Venta registrada",
            f"Venta de '{resultado['descripcion']}' registrada.\n"
            f"Total: ${resultado['total']:.2f}\n"
            f"Ganancia: ${resultado['ganancia']:.2f}\n"
            f"Piezas restantes: {resultado['disponibles_restantes']}",
        )

        self.app.refrescar_todo()

        self.catalogo = core.obtener_existencias(self.app.conn)
        self.producto_seleccionado = None
        self.cantidad_var.set("")
        self.precio_var.set("")
        self.obs_text.delete("1.0", tk.END)
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
