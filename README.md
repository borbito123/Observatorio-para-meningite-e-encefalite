Os bancos de dados do DATASUS que são trabalhados neste programa: 
- **SINAN**: notificações/casos sobre determinados agravos (no caso, meningite)
- **SIM**: óbitos registrados
- **CIHA**: internações/atendimentos hospitalares e/ou ambulatorais.

Este aplicativo cumpre duas funções:

1) Baixar os dados do SINAN (meningite; anos 2007 a 2025), SIM (2007 a 2024) e CIHA (2011 a 2025) referentes ao município, ao estado do Rio de Janeiro e a todos os estados, e convertê-los para os respectivos formatos parquet e duckdb, para fins de análise epidemiológica.

2) Fornecer uma plataforma dinâmica de análise de dados via streamlit.

# Baixando os bancos de dados e convertendo-os
Ao extrair o arquivo "Scripts" em formato RAR, haverão scripts separados para as diferentes etapas - baixar os arquivos do datasus, processar e compilar o que foi baixado para o formato parquet e para o formato duckdb, separado por ano. Bastar executar os scripts. Preferiu-se não unificar os arquivos para que o usuário tenha liberdade de escolher o que baixar. 
Alternativamente, pode-se baixar os arquivos já compilados diretamente através dos "Banco de Dados" em formato .RAR.

Quando os bancos de dados em .dbc são convertidos para .parquet, alguns filtros são aplicados para restringir quais casos são relevantes para a análise epidemiológica da meningite, da encefalite e da meningoencefalite. Além disso, como os dados disponibilizados pelo CIHA são separados por mês para cada respectivo ano, optou-se por mesclar os meses referentes a um dado ano, com a finalidade de analisar mais facilmente os casos referentes a um dado ano.

Em um primeiro momento, os CID-10 utilizados eram os principais códigos diretamente associados a meningite: A17.0 , A39.0 , A87 , G00 , G01 , G02 , G03 , G04 e G05. Contudo, no SIM e na CIHA havia um problema importante: muitos CID-10 que descrevemmeningite, encefalite ou meningoencefalite aparecem como códigos próprios, e nãonecessariamente dentro dos grupos prefixados G00 a G05.
Por exemplo, B58.2 (meningoencefalite por Toxoplasma) e B01.1 (encefalite por varicela) deveriam ser considerados no recorte de análise neurológica infecciosa, mas podem aparecer como CID-10 avulsos nos bancos brutos. Para contornar esse impasse, foi feita uma busca
explícita por CID-10 que incluem meningite, encefalite e/ou meningoencefalite sem incluir outras condições de forma ampla demais. Desse modo, atualmente os CID-10 analisados incluem: 
- Prefixados: G00* , G01* , G02* , G03* , G04* , G05* , A83* , A84* , A85* , A86* , A87* , B06* .
- Avulsos/específicos: A17.0 , A22.8 , A32.1 , A39.0 , B00.3 , B00.4 , B01.0 , B01.1 , B02.0, B02.1 , B05.0 , B05.1 , B26.1 , B26.2 , B37.5 , B38.4 , B45.1 , B57.4 , B58.2 , B60.2.

Referências utilizadas:
http://www2.datasus.gov.br/cid10/V2008/WebHelp/g00_g09.htm
http://www2.datasus.gov.br/cid10/V2008/cid10.htm

# Em construção - Formulário Digital para Investigação de meningite 

Utilizando XLXsforms, criei um espelho da ficha de investigação de meningite elaborada pelo SINAN. O propósito foi me familiarizar com este formato de planilha e quais possibilidades ela proporciona.
No momento, o formuláro está plenamente funcional, apenas faltando alguns ajustes para aprimorar sua apresentação estética. Caso queira acesso aos dados de preenchimento, favor entrar em contato.

Link: https://ee.kobotoolbox.org/x/ifAQUhNw.
  
# Em construção - Painel Streamlit para análise do banco de dados = SINAN, SIM e CIHA

Este app em Python foi feito para análise epidemiológica a partir de arquivos `.parquet ou .duckdb` do DATASUS, com foco nos três bancos de dados supracitados.
Link para a versão no streamlti: https://fgwybuegynhnli87zeyurr.streamlit.app/

## _O que o app faz_

- Lê os parquets da release mais atual deste aplicativo (https://github.com/borbito123/Teste---Dados-Epidemiol-gicos-para-meningite-SINAN-CIHA-SIM---Rio-de-Janeiro/releases/tag/v1.0) e já os carrega automaticamente no programa. Cabe ao usuário escolher quais bancos de dados carregar. Atualmente são disponibilizados os dados referente ao estado do RJ e logo mais os bancos de todas as UFs juntas serão disponibilizados.
- Também aceita **upload** dos parquets / duckdbs que o usuário escolher.
- Fornece um breve dicionário operacional para guiar o usuário em relação aos campos mais relevantes para análise epidemiológica;
- Gera gráficos epidemiológicos interativos.
- Permite download em CSV das tabelas agregadas de cada gráfico.

_Observação: Para contornar eventuais problemas de memória ou crashes do aplicativo, foram impostas algumas limitações que podem ser modificadas pelo usuário. No canto esquerdo da aba "Orientação" há a opção "desempenho e memória" que permite ajustar essas limitações._

## _Gráficos incluídos_

### Para SINAN
- Indicadores -> Fornece: tabela anual de indicadores; evolução dos casos confirmados; notificações, confirmados, descartados e óbitos; proporções, inconclusivos, ignorados e letalidade; hospitalização em suspeitos/ notificados, confirmados e descartados; prevalência de sinais/sintomas entre os registros; número de comunicantes por quimioprofilaxia; vacinação por classificação final; punção laboratorial; exame quimiocitológico do líquor (LCR); distribuição de glicose, proteínas, neutrófilos e leucócitos; resumo dos parâmetros do LCR.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidade ano × mês; estratificação por sexo, CID-10 convertido, grupo etiológico SINAN ou CLASSI_FIN , quando os campos estiverem disponíveis. 
- Análise do CID-10 -> Fornece: distribuição dos casos por classificação final (confirmado, descartado...), distribuição dos casos por conclusão diagnóstica (especifica o grupo etiológico), conversão dos grupos etiológicos preenchidos no banco de dados para os devidos CID-10 (o streamlt mostra a regra usada para converter CON_DIAGES em CID-10), distribuição dos casos conforme evolução, distribuição dos casos por critério diagnóstico utilizado, distribuição conforme realiização de punção laboratorial, gráficos de distribuição dos principais parâmetros liquóricos analisados (glicose, leucócitos, proteínas, neutrófiilos).
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; escolaridade de confirmados e óbitos; distribuição por sexo, raça/cor, município de residência e município de ocorrência/notificação. Nos gráficos municipais, é usado Top N + “Outros municípios”.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no SINAN:_ Originalmente, o SINAN agrupa todos os seus casos sob o CID "G03.9". Caso haja diagnóstico e confirmação, então se especifica a meningite em algumas categorias (veja a seção "Classificação do Caso" em https://portalsinan.saude.gov.br/images/documentos/Agravos/Meningite/Meningite_v5.pdf). Na seção "Análise epidemiológico e CID10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida.

A referência utilizada para alocação foi: https://portalsinan.saude.gov.br/images/documentos/Agravos/Meningite/Meningite_v5.pdf.

### Para SIM
- Indicadores -> Fornece: óbitos com menção de meningite/encefalite; óbitos com meningite/encefalite como causa básica; comparação entre menção e causa básica; óbito na gravidez ( OBITOGRAV ) por menção e por causa básica; óbito no puerpério ( OBITOPUERP ) por menção e por causa básica, quando o campo existir.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidadeano × mês; estratificação por sexo ou tipo CID-10 quando disponível.
- Análise do CID-10 -> Fornece: distribuição dos óbitos conforme o CID-10, gráfico que converte os CID-10 para o padrão utilizado no gráfico de conversão do SINAN. 
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; escolaridade; distribuição por sexo, raça/cor, município de residência e município de ocorrência.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no SIM:_ Por conta do jeito que o banco de dados é preenchido e disponiblizado, muitos CIDs que são incluídos em um dos CIDs prefixados (G00, G01, G02, G03, G04, G05) ficariam perdidos se o script de conversão não procurasse por eles explicitamente. Desse modo, os novos CIDs mencionados na seção "Baixando os bancos de dados e convertendo-os" deste readme.md foram inclusos para evitar que não fossem perdidos. Na seção "Análise etiológica e CID-10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida. 

### Para CIHA
- Indicadores -> Fornece: Fornece: total de atendimentos; atendimentos com diagnóstico principal de meningite/encefalite; mortes administrativas; modalidade hospitalar ou ambulatorial; procedimentos e quantidade; distribuição dos dias de permanência.
- Análise temporal -> Fornece: série temporal por ano, mês ou semana; heatmap de sazonalidade ano × mês; estratificação por sexo ou tipo CID-10 quando disponível. 
- Análise do CID-10 -> Fornece: distribuição dos atendimentos por tipo CID-10; conversão para adequação ao CID-10 de meningite/encefalite; verificação específica de G01 e G02 ; CID-10 dos registros com morte administrativa.
- Demografia -> Fornece: distribuição por faixa etária de 5 anos; pirâmide etária por sexo; distribuição por sexo, raça/cor, município de residência e município de ocorrência/atendimento.
- Campos importantes não preenchidos -> Fornece: quantos registros não foram preenchidos conforme certas variáveis de maior relevância
- Prévia -> Fornece: prévia do dados presentes no banco de dados, sendo possível exportar para o formato .CSV

_Explicando o que foi feito na tabela de conversão encontrada no CIHA:_ Por conta do jeito que o banco de dados é preenchido e disponiblizado, muitos CIDs que são incluídos em um dos CIDs prefixados (G00, G01, G02, G03, G04, G05) ficariam perdidos se o script de conversão não procurasse por eles explicitamente. Desse modo, os novos CIDs mencionados na seção "Baixando os bancos de dados e convertendo-os" deste readme.md foram inclusos para evitar que não fossem perdidos. Na seção "Análise etiológica e CID-10", haverá um gráfico de conversão que aloca todos os casos confirmados e os enquadra em algum dos seguintes CID: G00, G01, G02, G03, G04, G05, A39, A17, A87.

A se ponderar: A17 e A39 se enquadrariam no CID G01, mas atualmnte se encontram separadas. Em contrapartida, meningite por haemophilus e meningocóccica já foram incluídas no CID G00. Isso representaria uma certa inconsistência que precisaria ser corrigida. 

### Comparação entre bancos de dados
- Comparação temporal (semanas, meses, anos)
- Possibilidade de estratiificar por CID-10, utilizando os gráficos convertidos para facilitar a equivalência.

Observação: A comparação entre bases é **exploratória** e faz mais sentido quando o agravo, o território e a janela temporal são os mesmos.

# _Instalação_

Crie e ative um ambiente virtual, se desejar, e depois instale as dependências:

```bash
pip install -r requirements.txt
```

## _Execução_

No diretório do projeto, rode:

```bash
streamlit run app_streamlit_app.py
```

## _Como usar_

Disclaimer: Atenção ao limite de parquets / duckdbs que estão sendo lidos ao mesmo tempo em uma única seção. Isso pode ser alterado, mas bastante cuidado em quantos arquivos são carregados simultaneamente.

### Opção 1: leitura automática dos parquets disponibilizados na release mais atual do github
Basta selecionar quais anos deseja-se analisar. 

### Opção 2: upload
Envie um ou mais arquivos `.parquet ou .duckdb` na respectiva aba do banco de dados desejado.
