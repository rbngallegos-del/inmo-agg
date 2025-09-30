from fastapi import FastAPI, Query
# Por ahora, devolvemos datos de prueba para validar el flujo.


def demo_adapter():
return [
Listing(
    id="demo1",
    title="Depto 2 rec Col. Primavera",
    price=9000,
    bedrooms=2,
    bathrooms=1,
    type="departamento",
    furnished=False,
    location_city="Tampico",
    location_colonia="Primavera",
    url="https://ejemplo.local/depto-primavera",
    source="demo",
    photos=[],
),
Listing(
    id="demo2",
    title="Casa 2 rec Unidad Nacional",
    price=10000,
    bedrooms=2,
    bathrooms=1.5,
    type="casa",
    furnished=True, # esta no debe pasar si furnished=False
    location_city="Ciudad Madero",
    location_colonia="Unidad Nacional",
    url="https://ejemplo.local/casa-un",
    source="demo",
    photos=[],
),
]


@app.get("/search", response_model=SearchResponse)
async def search(
    cities: Optional[str] = Query(
        None, description="Ciudades separadas por coma. Ej: 'Tampico,Ciudad Madero,Altamira'"
        ),
        min_price: int = Query(0, ge=0),
        max_price: int = Query(10**9, ge=0),
        min_bedrooms: int = Query(0, ge=0),
        furnished: Optional[bool] = Query(None, description="True/False"),
        property_type: str = Query(
            "all", description="'casa', 'departamento' o 'all'", regex="^(casa|departamento|all)$"
            ),
            ):
        city_set = (
                {c.strip().lower() for c in cities.split(",")} if cities else None
                )


# 1) Llamar adapters en paralelo (aquí solo demo)
results = demo_adapter()


# 2) Filtrado
def keep(l: Listing) -> bool:
      if city_set and l.location_city.lower() not in city_set:
           return False
      if not (min_price <= l.price <= max_price):
           return False
      if l.bedrooms < min_bedrooms:
           return False
      if furnished is not None and l.furnished != furnished:
           return False
      if property_type != "all" and l.type != property_type:
           return False
      return True


filtered = [l for l in results if keep(l)]
return SearchResponse(results=filtered, total=len(filtered))