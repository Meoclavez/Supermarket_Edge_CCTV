"""IGA Pearcedale store map from the hand sketch (2026-10-06), in metres.

Origin top-left = back-left corner (cool room); x right, y down to the front
wall with the doors. The sketch is not to scale: sizes are standard fixtures
(gondola 1.2 m deep, aisles 2.0 m, back/front cases 1.2-1.4 m, aisles 12 m).
Positions along walls are interpolated from the sketch between gondola edges.

Writes store_map.json: {"layout": {...}, "structures": [...], "zones": [...]}.
"""

import json
from bisect import bisect_right

W, H = 36.0, 21.0
GL = 12.0                     # gondola length
GY0, GY1 = 3.5, 3.5 + GL      # gondola run (y)
BACK = 1.2                    # back wall case depth
FRONT_Y = 21.0

# Right sheet: sketch px x -> metres, anchored on fixture edges.
ANCH = [(535, 13.4), (612, 15.0), (760, 16.2), (808, 18.2), (950, 19.4), (980, 21.4), (1130, 22.6),
        (1190, 24.6), (1340, 25.8), (1385, 27.8), (1535, 29.0), (1560, 31.0), (1600, 32.2)]


def px(x):
    xs = [a for a, _ in ANCH]
    i = max(1, min(len(ANCH) - 1, bisect_right(xs, x)))
    (x0, m0), (x1, m1) = ANCH[i - 1], ANCH[i]
    return round(m0 + (x - x0) * (m1 - m0) / (x1 - x0), 2)


def rect(x0, y0, x1, y1):
    return [{"x": round(x0, 2), "y": round(y0, 2)}, {"x": round(x1, 2), "y": round(y0, 2)},
            {"x": round(x1, 2), "y": round(y1, 2)}, {"x": round(x0, 2), "y": round(y1, 2)}]


structures, zones = [], []


def S(kind, name, poly, **kw):
    structures.append({"kind": kind, "name": name, "polygon": poly, **kw})


def Z(name, category, poly, products=(), color=None):
    z = {"name": name, "category": category, "polygon": poly, "products": list(products)}
    if color:
        z["color"] = color
    zones.append(z)


# ---------------------------------------------------------------- rooms/walls
S("ROOM", "Cool room", rect(0, 0, 6.1, 4.7))
S("ROOM", "Bottle shop", rect(0, 4.7, 6.1, H))
S("ROOM", "Not on photo: check", rect(34.2, 0, W, H), color="#6b7280")
S("DOOR", "Cool room door", [{"x": 4.6, "y": 4.7}, {"x": 5.6, "y": 4.7}], thickness_m=0.15)
S("DOOR", "Main doors", [{"x": 13.6, "y": FRONT_Y}, {"x": 16.0, "y": FRONT_Y}], thickness_m=0.2)

# ------------------------------------------------------------- bottle shop
S("SHELF", "Pinot noir", rect(0.8, 6.0, 1.4, 18.0))
S("SHELF", "Shiraz", rect(1.4, 6.0, 2.0, 18.0))
S("SHELF", "10 packs", rect(5.4, 4.8, 6.0, 10.6))
S("SHELF", "Casks", rect(5.4, 10.6, 6.0, 13.3))
S("SHELF", "Fortified", rect(5.4, 13.3, 6.0, 16.0))
S("SHELF", "Spirits", rect(0.8, 20.3, 5.0, FRONT_Y))

# ------------------------------------------ produce wall (west of the island)
for name, y0, y1 in (("Stationery · Cards", 2.8, 4.7), ("Bread", 4.7, 7.0), ("Eggs", 7.0, 9.4),
                     ("Produce", 9.4, 11.9), ("Produce", 11.9, 16.0)):
    S("SHELF", name, rect(6.2, y0, 7.4, y1))

# --------------------------------------------------------- produce island
S("SHELF", "Wraps", rect(9.6, 4.8, 10.8, 6.8))
S("SHELF", "Snacks", rect(10.8, 4.8, 12.0, 6.8))
S("SHELF", "Cakes", rect(9.6, 6.8, 10.8, 8.2))
S("SHELF", "Unclear label: check", rect(10.8, 6.8, 12.0, 8.2))
S("SHELF", "Produce", rect(9.6, 8.2, 12.0, 15.0))

# ----------------------------------------------------- checkouts, front wall
S("COUNTER", "Reg 3", rect(7.8, 16.4, 9.4, 17.2))
S("COUNTER", "Reg 2", rect(11.6, 16.4, 13.2, 17.2))
S("COUNTER", "Reg 1", rect(12.4, 18.0, 13.2, 19.8))
S("SHELF", "Cigarettes", rect(8.6, 20.3, 12.4, FRONT_Y))
S("SHELF", "Fridge: ready meals", rect(px(760), 19.8, px(910), FRONT_Y))
S("COUNTER", "Hot chickens", rect(px(910), 19.6, px(975), FRONT_Y))
S("COUNTER", "Deli", rect(px(975), 19.6, px(1210), FRONT_Y))
S("COUNTER", "Deli", rect(px(1210), 19.6, px(1500), FRONT_Y))
S("COUNTER", "Chicken oven", rect(px(1500), 19.6, 34.2, FRONT_Y))

# ---------------------------------------------------- back wall cases (px)
BACK_CASES = [(535, 600, "Stationery"), (600, 670, "Ice cream"), (670, 815, "Ice cream"),
              (815, 975, "Frozen meals · Pizza · Pies"), (975, 1100, "Frozen chicken · Fish · Chips"),
              (1100, 1225, "Frozen veg"), (1225, 1300, "Fresh dog food"), (1300, 1410, "Butter"),
              (1410, 1600, "Yoghurt · Cream")]
for x0, x1, name in BACK_CASES:
    S("SHELF", name, rect(px(x0), 0, px(x1), BACK))
S("SHELF", "Dairy (continues, not on photo)", rect(32.2, 0, 34.2, BACK))

# ---------------------------------------------------------------- gondolas
# (x_left_m, west-side sections, east-side sections); sections = (name, f0, f1)
# with f = fraction of the gondola length from the back.
GONDOLAS = [
    (15.0, [("Spreads · Multi-pack snacks", 0, .17), ("Gluten free", .17, .30),
            ("Biscuits sweet", .30, .65), ("Biscuits savoury", .65, 1)],
           [("Water · Energy drinks", 0, .30), ("Soft drink", .30, 1)]),
    (18.2, [("Chips", 0, .34), ("Lollies", .34, .65), ("Chocolate", .65, 1)],
           [("Long life milk", 0, .16), ("Coffee", .16, .30), ("Tea", .30, .42),
            ("Porridge · Muesli bars", .42, .65), ("Cereal", .65, 1)]),
    (21.4, [("Canned fruit", 0, .16), ("Baking needs · Cordial", .16, .32), ("Cake mix", .32, .49),
            ("Sugar · Flour", .49, .66), ("Fruit juice", .66, 1)],
           [("Soups", 0, .18), ("Meal bases · Mexican", .18, .31), ("Canned fish", .31, .48),
            ("Canned beans", .48, .66), ("Mayonnaise", .66, .83), ("Sauces", .83, 1)]),
    (24.6, [("Oils", 0, .12), ("Noodles", .12, .25), ("Indian", .25, .45),
            ("Gravy · Herbs · Stocks", .45, .59), ("Pasta & sauce", .59, .79), ("Canned veg", .79, 1)],
           [("Soap", 0, .13), ("Shampoo", .13, .29), ("Shampoo · Hair dye", .29, .45),
            ("Razors · Shaving", .45, .59), ("Dental", .59, .72), ("Medical", .72, 1)]),
    (27.8, [("Insect control", 0, .08), ("Cat food", .08, .36), ("Animal needs", .36, .47),
            ("Dog food dry", .47, .62), ("Dog food wet", .62, .78), ("Animal snacks", .78, 1)],
           [("Wrap · Foil · Bags", 0, .18), ("Kitchen & bathroom cleaning", .18, .47),
            ("Paper towel", .47, .62), ("Softener", .62, .78), ("Laundry detergent", .78, 1)]),
    (31.0, [("Bulbs", 0, .3), ("Home products", .3, 1)],
           [("Not on photo: check", 0, 1)]),
]
for xl, west, east in GONDOLAS:
    for side, x0, x1 in (("W", xl, xl + 0.6), ("E", xl + 0.6, xl + 1.2)):
        for name, f0, f1 in (west if side == "W" else east):
            S("SHELF", name, rect(x0, GY0 + f0 * GL, x1, GY0 + f1 * GL), properties={"side": side})


def names(sections):
    out = []
    for n, *_ in sections:
        for part in n.replace(" & ", "|&|").split(" · "):
            part = part.replace("|&|", " & ")
            if "check" not in part and part not in out:
                out.append(part)
    return out


# ------------------------------------------------------------------- zones
# Non-overlapping (the live pipeline takes the first zone that contains a point).
G = {g[0]: g for g in GONDOLAS}
Z("Produce wall aisle", "AISLE", rect(7.4, 1.2, 9.6, 16.0),
  ["Stationery", "Cards", "Bread", "Eggs", "Produce", "Wraps", "Cakes"])
Z("Aisle 1 · Produce / Biscuits", "AISLE", rect(12.0, GY0, 15.0, GY1),
  ["Produce", "Snacks"] + names(G[15.0][1]))
Z("Aisle 2 · Drinks / Chips", "AISLE", rect(16.2, GY0, 18.2, GY1), names(G[15.0][2]) + names(G[18.2][1]))
Z("Aisle 3 · Breakfast / Baking", "AISLE", rect(19.4, GY0, 21.4, GY1), names(G[18.2][2]) + names(G[21.4][1]))
Z("Aisle 4 · Canned / Pasta", "AISLE", rect(22.6, GY0, 24.6, GY1), names(G[21.4][2]) + names(G[24.6][1]))
Z("Aisle 5 · Health / Pets", "AISLE", rect(25.8, GY0, 27.8, GY1), names(G[24.6][2]) + names(G[27.8][1]))
Z("Aisle 6 · Cleaning / Home", "AISLE", rect(29.0, GY0, 31.0, GY1), names(G[27.8][2]) + names(G[31.0][1]))
Z("Back aisle · Frozen", "DEPARTMENT", rect(9.6, BACK, px(1225), GY0),
  ["Stationery", "Ice cream", "Frozen meals", "Pizza", "Pies", "Frozen chicken", "Fish", "Chips", "Frozen veg"])
Z("Back aisle · Dairy", "DEPARTMENT", rect(px(1225), BACK, 34.2, GY0),
  ["Fresh dog food", "Butter", "Yoghurt", "Cream"])
Z("Checkouts", "CHECKOUT", rect(7.4, 16.0, 13.4, FRONT_Y), ["Cigarettes"])
Z("Front walkway", "AISLE",
  [{"x": 12.0, "y": GY1}, {"x": 34.2, "y": GY1}, {"x": 34.2, "y": 17.6}, {"x": 13.4, "y": 17.6},
   {"x": 13.4, "y": 16.0}, {"x": 12.0, "y": 16.0}])
Z("Entrance", "ENTRANCE", rect(13.4, 17.6, px(760), FRONT_Y))
Z("Ready meals · Hot chickens", "DEPARTMENT", rect(px(760), 17.6, px(975), 19.6),
  ["Ready meals", "Hot chickens"])
Z("Deli", "DEPARTMENT", rect(px(975), 17.6, 34.2, 19.6), ["Deli", "Chicken"])
Z("Bottle shop", "DEPARTMENT", rect(0, 4.7, 6.1, FRONT_Y),
  ["Pinot noir", "Shiraz", "10 packs", "Casks", "Fortified", "Spirits"])
Z("Cool room", "DEPARTMENT", rect(0, 0, 6.1, 4.7))

if __name__ == "__main__":
    out = {"layout": {"name": "IGA Pearcedale", "width_m": W, "height_m": H},
           "structures": structures, "zones": zones}
    json.dump(out, open("store_map.json", "w"), indent=1)
    print(f"{len(structures)} structures, {len(zones)} zones")
    for z in zones:
        print(f"  {z['category']:<10} {z['name']:<32} {', '.join(z['products'])[:110]}")
