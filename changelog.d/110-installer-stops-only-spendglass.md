- The supervisor installer stops only a copy of Spendglass on port 8903
  (#110). If another program holds the port, it changes nothing and
  tells you which one, so you can stop it yourself. Before, it stopped
  whatever was listening there.
