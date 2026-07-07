PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
IS_DIRECT = (PORT == 8769)  # Direktzugriff ohne Ingress
