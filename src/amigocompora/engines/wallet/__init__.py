"""El motor de cartera: leer lo que hay, en cualquier red, sin firmar nada.

Este paquete lee. No firma, no emite, no pide claves. Mover dinero vive en el
camino de ejecución, que pasa por la barrera de modo y por el diálogo de
confirmación; aquí no hay ninguna función que acepte una clave privada.

Los dos lectores —`evm` y `solana`— no son dos motores: son las dos familias de
red que hay, y el motor elige uno u otro según el formato de dirección de la red.
Se separan en dos módulos porque no comparten **nada** de cómo se lee un saldo:
EVM necesita Multicall3 y una lista de contratos que alguien haya nombrado, y
Solana enumera las cuentas de la propia dirección. Un módulo único tendría un
`if` en cada función.
"""
