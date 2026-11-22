@echo off
setlocal

REM ===========================================================================
REM  Duplicata quotidien des documents + sauvegardes vers le partage reseau.
REM  Tache planifiee : CiblesDocsMirror (voir docs/inventaire-cibles.md).
REM
REM  /!\ SENS UNIQUE : serveur  ->  partage.
REM      /MIR PURGE la destination. Inverser SRC et DST detruirait les
REM      documents de production. Ne jamais permuter les deux lignes.
REM
REM  /!\ /MIR est un MIROIR, pas une archive : un fichier supprime cote serveur
REM      disparait du partage au passage suivant. C'est voulu (rien dans
REM      l'application ne peut supprimer un fichier), mais ce n'est PAS une
REM      protection contre la suppression accidentelle.
REM
REM  /!\ Compte d'execution : sous SYSTEM, la machine s'authentifie en tant que
REM      <MACHINE>$. Sur un chemin UNC en adresse IP, Kerberos ne s'applique pas
REM      et la retombee NTLM est incertaine. Verifier l'acces AVANT d'enregistrer
REM      la tache (voir la procedure dans la doc), sinon la tache echouera
REM      silencieusement alors qu'un test en session interactive passe.
REM ===========================================================================

set "SRC=%~dp0.."
set "DST=\\YOUR-SERVER\share\inventaire_cibles"
set "LOG=%SRC%\backups\mirror_documents.log"

if not exist "%SRC%\backups" mkdir "%SRC%\backups"

REM Le dossier des bons de commande n'existe qu'apres le premier depot.
if not exist "%SRC%\BC_cibles" mkdir "%SRC%\BC_cibles"

echo === %DATE% %TIME% : demarrage du miroir vers %DST% > "%LOG%"

REM /MIR miroir  /FFT tolere la granularite 2s des horodatages SMB (sans lui
REM robocopy recopie tout a chaque passage)  /R:2 /W:5 peu de reprises
REM /NP /NDL journal compact  /LOG+ ajoute a la suite du premier appel.
robocopy "%SRC%\SDS_cibles" "%DST%\SDS_cibles" /MIR /FFT /R:2 /W:5 /NP /NDL /LOG+:"%LOG%" /TEE
if %ERRORLEVEL% GEQ 8 goto :fail

robocopy "%SRC%\COA_cibles" "%DST%\COA_cibles" /MIR /FFT /R:2 /W:5 /NP /NDL /LOG+:"%LOG%" /TEE
if %ERRORLEVEL% GEQ 8 goto :fail

robocopy "%SRC%\BC_cibles" "%DST%\BC_cibles" /MIR /FFT /R:2 /W:5 /NP /NDL /LOG+:"%LOG%" /TEE
if %ERRORLEVEL% GEQ 8 goto :fail

REM Les sauvegardes SQLite partent avec : un partage plein de PDF sans les tables
REM material_sds / coa_link / bc_link obligerait a refaire tout le liage manuel.
robocopy "%SRC%\backups" "%DST%\backups" /MIR /FFT /R:2 /W:5 /NP /NDL /XF mirror_documents.log /LOG+:"%LOG%" /TEE
if %ERRORLEVEL% GEQ 8 goto :fail

echo === %DATE% %TIME% : miroir termine sans erreur >> "%LOG%"
REM Robocopy renvoie un masque de bits : 0-7 = succes (1 copie, 2 extra, 3 les
REM deux...), >= 8 = echec. Sans cette normalisation, le Planificateur afficherait
REM LastTaskResult=1 ou 3 en regime NORMAL et on le lirait comme un echec.
exit /b 0

:fail
echo === %DATE% %TIME% : ECHEC robocopy, code %ERRORLEVEL% >> "%LOG%"
exit /b 8
