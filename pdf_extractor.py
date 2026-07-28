"""Extraccion pura de remisiones PDF de Betterware.

Este modulo contiene unicamente la logica de parseo: recibe rutas de PDF
y devuelve `list[dict]`. No conoce hojas de calculo, ni base de datos, ni
ningun otro medio de almacenamiento.

Las ocho primeras funciones se copiaron VERBATIM desde
`reference/inventario_core.py` (ADR-4: el riesgo de parseo debe quedarse
en cero), por lo que su estilo no sigue todavia `.langs/python.md` (sin
type hints, sin complejidad en docstrings). Esa adecuacion esta diferida
a proposito.

El bloque final (`extraer_puntos_bw`, `extraer_semana_cierre_bw`,
`extraer_puntos_de_paginas`, BW-02) es codigo nuevo y si sigue el
estandar. Solo ANADE funciones: `procesar_pdf` y el resto del bloque
verbatim quedan intactas, porque el golden snapshot de FUND-01
(`tests/baseline/C001264_NOTA.baseline.json`) las cubre.

Las expresiones regulares del bloque nuevo van embebidas en cada funcion
en lugar de vivir como constantes `Final` de modulo: la suite de FUND-03
(`test_module_defines_no_module_level_constant`) fija como invariante que
este modulo no declare estado a nivel de modulo. `re` cachea los patrones
compilados, asi que no hay coste por llamada.
"""

import os
import re
from datetime import datetime

import pdfplumber


def _clean_money(value):
    if value is None:
        return 0.0
    value = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(value)
    except ValueError:
        return 0.0


def _clean_int(value):
    if value is None:
        return 0
    value = str(value).strip()
    try:
        return int(value)
    except ValueError:
        return 0


def extraer_metadata(pagina_texto):
    meta = {
        "Semana": "",
        "Folio de pedido": "",
        "Codigo nota": "",
        "Distribuidora": "",
        "Nombre asociado": "",
    }

    m = re.search(r"Semana\s+([\d\s\-]+\d)", pagina_texto)
    if m:
        meta["Semana"] = m.group(1).strip()

    m = re.search(r"Folio de pedido\s+(\S+)", pagina_texto)
    if m:
        meta["Folio de pedido"] = m.group(1).strip()

    m = re.search(r"C[oó]digo\s+(\d+)\s+Nombre Asociado", pagina_texto)
    if m:
        meta["Codigo nota"] = m.group(1).strip()

    m = re.search(r"Distribuidora\s+(C\d+[^\n]*)", pagina_texto)
    if m:
        meta["Distribuidora"] = m.group(1).strip()

    m = re.search(r"Nombre Asociado\s+(.+?)\s+Tel[eé]fono Distribuidora", pagina_texto)
    if m:
        meta["Nombre asociado"] = m.group(1).strip()

    return meta


def extraer_productos_de_tabla(tabla):
    productos = []
    for row in tabla:
        # OJO: solo se descartan las celdas que son None (huecos de la
        # tabla). Las celdas vacias pero reales (como "Pag" en blanco
        # cuando un producto no trae numero de pagina) SE CONSERVAN
        # como texto vacio, para no perder columnas y que la fila no
        # se descarte por error.
        limpio = [str(c).strip().replace("\n", " ") if c is not None else None for c in row]
        limpio = [c for c in limpio if c is not None]
        if not limpio:
            continue
        primero = limpio[0]
        if primero in ("Artículo", "Articulo") or primero.startswith("Total"):
            continue
        if not re.match(r"^\d{4,7}$", primero):
            continue

        if len(limpio) == 8:
            codigo, desc, pag, solicitada, surtida, sin_iva, con_iva, valor_total = limpio
            cantidad_surtida = _clean_int(surtida)
            precio_con_iva_unit = _clean_money(con_iva)
            precio_que_pagas = round(precio_con_iva_unit * cantidad_surtida * (1 - 0.18), 2)
            productos.append({
                "Codigo articulo": codigo,
                "Descripcion": desc,
                "Cantidad solicitada": _clean_int(solicitada),
                "Cantidad surtida": cantidad_surtida,
                "Cantidad Asociado": 0,
                "Cantidad Casa": cantidad_surtida,
                "Cantidad Local": 0,
                "Precio catalogo": _clean_money(sin_iva),
                "Precio con IVA": precio_con_iva_unit,
                "Precio que pagas": precio_que_pagas,
                "Valor total con IVA": _clean_money(valor_total),
                "Tipo": "Normal (con descuento)",
            })
        elif len(limpio) == 9:
            codigo, desc, solicitada, surtida, catalogo, paga, gana, con_iva, valor_total = limpio
            cantidad_surtida = _clean_int(surtida)
            productos.append({
                "Codigo articulo": codigo,
                "Descripcion": desc,
                "Cantidad solicitada": _clean_int(solicitada),
                "Cantidad surtida": cantidad_surtida,
                "Cantidad Asociado": 0,
                "Cantidad Casa": cantidad_surtida,
                "Cantidad Local": 0,
                "Precio catalogo": _clean_money(catalogo),
                "Precio con IVA": _clean_money(con_iva),
                "Precio que pagas": _clean_money(paga),
                "Valor total con IVA": _clean_money(valor_total),
                "Tipo": "Sin descuento",
            })
    return productos


def procesar_pdf(ruta_pdf):
    """Procesa un PDF que puede traer un pedido o varios (uno por
    pagina). Cada pagina se revisa por separado: si trae su propio
    encabezado (Semana/Folio de pedido), se toma como un pedido nuevo;
    si no trae encabezado, se asume que es continuacion del pedido de
    la pagina anterior (un mismo pedido repartido en varias paginas)."""
    filas = []
    meta_actual = {
        "Semana": "",
        "Folio de pedido": "",
        "Codigo nota": "",
        "Distribuidora": "",
        "Nombre asociado": "",
    }
    with pdfplumber.open(ruta_pdf) as pdf:
        for num_pagina, pagina in enumerate(pdf.pages, start=1):
            texto_pagina = pagina.extract_text() or ""
            meta_pagina = extraer_metadata(texto_pagina)

            if meta_pagina["Folio de pedido"]:
                meta_actual = meta_pagina

            for tabla in pagina.extract_tables():
                if not tabla:
                    continue
                primera_fila = [str(c).strip() if c else "" for c in tabla[0]]
                if not any("Art" in c for c in primera_fila):
                    continue
                for prod in extraer_productos_de_tabla(tabla):
                    fila = {
                        "Fecha registro": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "Semana": meta_actual["Semana"],
                        "Folio de pedido": meta_actual["Folio de pedido"],
                        "Codigo nota": meta_actual["Codigo nota"],
                        "Distribuidora": meta_actual["Distribuidora"],
                        "Nombre asociado": meta_actual["Nombre asociado"],
                        "Archivo origen": f"{os.path.basename(ruta_pdf)} (pag. {num_pagina})",
                    }
                    fila.update(prod)
                    filas.append(fila)

    # Si un mismo producto aparece mas de una vez en el mismo pedido
    # (por ejemplo, se agrego dos veces al carrito), cada aparicion se
    # numera. Esto evita que, al guardar, se confunda con un duplicado
    # y se pierda una de las piezas.
    contador_ocurrencias = {}
    for fila in filas:
        clave = (fila["Folio de pedido"], fila["Codigo articulo"], fila["Tipo"])
        contador_ocurrencias[clave] = contador_ocurrencias.get(clave, 0) + 1
        fila["Ocurrencia"] = contador_ocurrencias[clave]

    return filas


def preparar_filas_desde_pdfs(rutas_pdf):
    """Extrae los productos de uno o varios PDF SIN guardarlos todavia
    en el Excel, para poder mostrarlos en una vista previa editable
    antes de confirmar la carga. Regresa (lista_de_filas, errores)."""
    todas_las_filas = []
    errores = []
    for ruta in rutas_pdf:
        try:
            filas = procesar_pdf(ruta)
            if not filas:
                errores.append(f"{os.path.basename(ruta)}: no se encontraron productos (revisa que sea una remision valida)")
            todas_las_filas.extend(filas)
        except Exception as e:
            errores.append(f"{os.path.basename(ruta)}: error al leer el archivo ({e})")
    return todas_las_filas, errores


def limpiar_telefono(telefono):
    """Deja solo digitos y antepone codigo de pais (52, Mexico) si
    parece un numero local de 10 digitos."""
    digitos = re.sub(r"\D", "", str(telefono or ""))
    if len(digitos) == 10:
        digitos = "52" + digitos
    return digitos


def link_whatsapp(telefono, mensaje=""):
    """Regresa el link de wa.me listo para abrir en el navegador con
    un mensaje precargado (opcional)."""
    numero = limpiar_telefono(telefono)
    if not numero:
        return None
    if mensaje:
        from urllib.parse import quote
        return f"https://wa.me/{numero}?text={quote(mensaje)}"
    return f"https://wa.me/{numero}"


# ---------------------------------------------------------------------
# BW-02 — puntos Betterware (codigo nuevo, no verbatim)
# ---------------------------------------------------------------------


def extraer_puntos_bw(pagina_texto: str) -> int | None:
    """Extrae el `Total PB acumulados` de una pagina de la nota (R1, R2).

    Funcion pura y total: **nunca lanza**. Una pagina sin la frase, en blanco o
    `None` devuelve `None`, que es la senal de "esta pagina no reporta puntos" y
    lo que impide que el flujo de carga toque la semana (R7).

    El numero llega con separador de miles del PDF (`"20,003"`), asi que las
    comas se eliminan antes de convertir. El patron acepta `[\\d,]+` para no
    cortar el numero en la primera coma; el `int()` va protegido porque ese
    patron tambien casaria una cadena de comas sin digitos.

    Args:
        pagina_texto: texto plano de una pagina, tal como lo entrega
            `pdfplumber.Page.extract_text()`.

    Returns:
        Los puntos acumulados como `int`, o `None` si la pagina no los reporta.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    coincidencia = re.search(r"Total PB acumulados\s+([\d,]+)", pagina_texto or "")
    if coincidencia is None:
        return None

    try:
        return int(coincidencia.group(1).replace(",", ""))
    except ValueError:
        return None


def extraer_semana_cierre_bw(pagina_texto: str) -> int | None:
    """Extrae la semana de cierre a la que pertenecen los puntos (R3).

    Betterware reporta el acumulado *al cierre de la semana N*: esa `N` es la
    semana **autoritativa** de los puntos, y no tiene por que coincidir con la
    `Semana` del pedido (que es cuando se levanto la orden). En el PDF de
    muestra los pedidos son de la semana 30 y los puntos dicen
    `al cierre de semana 29`.

    Funcion pura y total: **nunca lanza**; sin la frase devuelve `None` y el
    call-site cae al fallback de la semana del pedido (R5).

    Args:
        pagina_texto: texto plano de una pagina de la nota.

    Returns:
        El numero de semana de cierre como `int`, o `None` si no aparece.

    Time: O(n) sobre la longitud del texto | Space: O(1)
    """
    coincidencia = re.search(r"al cierre de semana\s+(\d+)", pagina_texto or "")
    if coincidencia is None:
        return None
    return int(coincidencia.group(1))


def extraer_puntos_de_paginas(ruta_pdf: str) -> list[tuple[int, int | None]]:
    """Recorre el PDF y devuelve los puntos de cada pagina que los reporte (R9, R7).

    Envoltorio a nivel PDF que reusa el patron de bucle de `procesar_pdf` sin
    tocarla (ADR-4). Las paginas sin `Total PB acumulados` **no aparecen** en el
    resultado, de modo que el flujo de carga no dispara ningun `UPDATE` por
    ellas (R7).

    Mantiene la restriccion de capa (R9): solo usa `re` y `pdfplumber`, sin
    arrastrar la capa de reportes ni la de persistencia.

    Args:
        ruta_pdf: ruta del PDF de remisiones a leer.

    Returns:
        Una lista de `(puntos, semana_cierre)` en orden de pagina.
        `semana_cierre` es `None` cuando la pagina reporta puntos pero no la
        frase de cierre.

    Raises:
        Los errores de apertura/lectura de `pdfplumber` se propagan: una ruta
        ilegible es un fallo del call-site, no un dato ausente.

    Time: O(p * n) | Space: O(p)  (p = paginas, n = texto por pagina)
    """
    puntos_por_pagina: list[tuple[int, int | None]] = []
    with pdfplumber.open(ruta_pdf) as pdf:
        for pagina in pdf.pages:
            texto_pagina = pagina.extract_text() or ""
            puntos = extraer_puntos_bw(texto_pagina)
            if puntos is None:
                continue
            puntos_por_pagina.append((puntos, extraer_semana_cierre_bw(texto_pagina)))
    return puntos_por_pagina
