// Porteiro do Zóio: caixa paramétrica para impressão 3D (OpenSCAD 2021.01 ou mais novo)
//
// Peças (cada uma vira um STL):
//   base     caixa de trás, que vai parafusada na parede
//   bandeja  chassi removível com os encaixes de todas as placas
//   tampa    frente do porteiro (grade do alto-falante, microfone e botão)
//   anel     anel que prende o alto-falante na tampa
//   capa     aba contra chuva, parafusada na parede logo acima da caixa
//
// Vistas de prévia: montagem, explodida, interior, tampa_dentro.
// Exportar uma peça já na posição de impressão:
//   openscad -o base.stl -D 'peca="base"' porteiro_caixa.scad
//
// Eixos: X largura, Y altura (de baixo para cima, com a caixa na parede),
// Z profundidade (0 = parede, cresce para a frente). Medidas em milímetros.

peca = "montagem"; // [montagem, explodida, interior, tampa_dentro, base, bandeja, tampa, anel, capa]

/* [Caixa] */
parede   = 3;     // espessura das paredes laterais
fundo    = 3;     // espessura do fundo (encosta na parede)
in_w     = 116;   // largura interna
in_h     = 206;   // altura interna
in_d     = 50;    // profundidade interna
raio     = 8;     // raio dos cantos por fora da base
folga    = 0.3;   // folga entre peças que se encaixam
saia_t   = 2.4;   // espessura da saia da tampa (abraça a base por fora)
saia_l   = 10;    // quanto a saia cobre a base
face_t   = 3;     // espessura da frente
chanfro  = 1.2;   // chanfro da borda da frente
lingua_t = 1.6;   // lingueta interna da tampa (labirinto contra água)
lingua_l = 6;
vedacao  = 1;     // vão para a fita de espuma entre a borda da base e a tampa

/* [Bandeja] */
bj_t     = 3;     // espessura da bandeja
bj_w     = 112;   // largura da bandeja
bj_y0    = 14;    // onde a bandeja fica, medido do chão interno (abaixo ficam os prensa-cabos)
bj_apoio = 2;     // altura dos apoios da bandeja sobre o fundo
desliza  = 7;     // quanto a bandeja desce para travar nos ganchos
standoff = 4;     // altura dos pinos das placas
piloto   = 2.5;   // furo para parafuso M3 autoatarraxante (plástico)

/* [Placa de extensão do ESP32-S3 (V2775): MEÇA a sua] */
ext_l     = 83;   // comprimento (no sentido do ESP32)
ext_w     = 63;   // largura
ext_furo  = 3.5;  // distância do centro do furo até a borda
// altura da pilha extensão + ESP32 encaixado (pinos, soquete, módulo)
ext_pilha = 15;

/* [Módulo relé de 4 canais 5 V] */
rele_l    = 75;
rele_w    = 55;
rele_furo = 2.75; // centro do furo até a borda (furos a 69,5 x 49,5)
rele_alt  = 17;   // placa + relés

/* [Regulador 12 V para 5 V (LM2596)] */
reg_l   = 43;
reg_w   = 21;
reg_alt = 14;

/* [Amplificador MAX98357A (fica em pé, na tampa)] */
amp_l = 17.8;  // lado que entra no suporte vertical
amp_h = 19.3;

/* [Rádio 433 MHz, opcional (fica em pé, na tampa)] */
rx_l = 30;     // receptor RXB6
rx_h = 14;
tx_l = 20;     // transmissor STX882
tx_h = 11;

/* [Conectores Wago 221 (largura x profundidade)] */
wago3 = [18.3, 18.6];   // 221-413 (3 fios): 12 V
wago5 = [29.9, 18.6];   // 221-415 (5 fios): 5 V e GND
wago_alt = 8.3;

/* [Alto-falante] */
fal_d     = 40;   // diâmetro (40 mm, 3 W é o mais comum; 50 também cabe)
fal_prof  = 16;   // profundidade atrás da borda
fal_borda = 2;    // espessura da borda que fica presa
fal_y     = 165;  // altura do centro (do chão interno)

/* [Microfone INMP441] */
mic_lado = 14.5;  // lado da plaquinha
mic_x    = -34;   // deslocamento do furo em relação ao centro (fica à esquerda dos botões)
mic_y    = 101;   // altura do furo (do chão interno)
mic_prof = 20;    // espaço atrás (plaquinha + pinos + conector)

/* [Botões da campainha (inox, de embutir), um por casa] */
botao_d    = 16;  // furo: 16 mm (botão de pulso inox antivandalismo, rosca M16); 19 e 22 também servem
botao_prof = 28;  // corpo atrás da frente, com os bornes de parafuso
botoes_y   = [114, 88];   // altura de cada botão (do chão interno): Casa 1 em cima, Casa 2 embaixo
botao_x    = -8;  // deslocamento da coluna de botões em relação ao centro
etiqueta   = [36, 14];   // rebaixo para a etiqueta de cada casa, à direita do botão (fita de 12 mm cabe)

/* [Prensa-cabos no fundo] */
pg_n    = 4;      // quantidade
pg_furo = 12.5;   // PG7 = 12,5; PG9 = 15,2; M16 = 16
pg_passo = 20;
pg_z    = 13;     // centro do furo, medido da parede
membrana = 0.6;   // furo "destacável": fica fechado até você furar
rotulos_pg = ["12V", "TRAVA", "GAR 1", "GAR 2", "RES", "RES"];

/* [Capa de chuva] */
capa_aba   = 24;  // quanto avança para a frente
capa_lado  = 70;  // altura das abas laterais
capa_placa = 40;  // placa de fixação acima da caixa

/* [Texto] */
logo = "Zóio";
fonte = "DejaVu Sans:style=Bold";

$fn = 64;
e = 0.01;

// ---------- medidas derivadas ----------
out_w  = in_w + 2*parede;
out_h  = in_h + 2*parede;
base_d = fundo + in_d;          // borda da frente da base
g      = folga + saia_t;        // quanto a tampa passa da base por lado
tw     = out_w + 2*g;
th     = out_h + 2*g;
tr     = raio + g;
z_face   = base_d + vedacao;     // face de dentro da tampa
z_frente = z_face + face_t;
cx = out_w/2;                   // centro em X
bx0 = parede + (in_w - bj_w)/2; // bandeja, canto em X
by0 = parede + bj_y0;           // bandeja, canto em Y (posição travada)
bz0 = fundo + bj_apoio;         // bandeja, face de baixo

// layout da bandeja (coordenadas locais)
rele_y  = 3;
baixa_y = rele_y + rele_w + 4;        // faixa baixa: regulador e conectores Wago
baixa_h = 48;
ext_y   = baixa_y + baixa_h + 4;
bj_h    = ext_y + ext_w + 3;
canal   = 11;                          // canais de cabo nas laterais

// ganchos (posição na bandeja), trava e parafusos da parede
ganchos = [[6.5, 20], [bj_w-6.5, 20], [6.5, ext_y + 36], [bj_w-6.5, ext_y + 36]];
trava   = [bj_w - 6, baixa_y + baixa_h + 2];
parafusos_parede = [[parede+7, parede+70], [out_w-parede-7, parede+70],
                    [parede+7, parede+196], [out_w-parede-7, parede+196]];
// parafusos de baixo que prendem a tampa
z_paraf = base_d - 5;
paraf_x = [parede + 7, out_w - parede - 7];

// conferências de espaço (aparecem no console)
topo_bj = bz0 + bj_t;
_esp  = topo_bj + standoff + 1.6 + ext_pilha;
_rele = topo_bj + standoff + rele_alt;
_reg  = topo_bj + 2 + reg_alt;
echo(str("Tamanho por fora: ", tw, " x ", th, " x ", z_frente, " mm (L x A x P)"));
echo(str("Folga ESP32 atrás do alto-falante: ", z_face - fal_prof - _esp, " mm"));
echo(str("Folga do botão sobre a faixa baixa: ", z_face - botao_prof - _reg, " mm"));
echo(str("Folga do relé até a tampa: ", z_face - _rele, " mm"));
echo(str("Bandeja: ", bj_w, " x ", bj_h, "; sobra em cima ao encaixar: ", in_h - bj_y0 - bj_h - desliza, " mm"));
assert(in_h - bj_y0 - bj_h - desliza >= 0, "A bandeja não cabe: aumente in_h");
assert(z_face - fal_prof - _esp >= 1, "O alto-falante encosta no ESP32: aumente in_d");
assert(z_face - botao_prof - _reg >= 1, "O botão encosta no regulador: aumente in_d");

// ---------- utilitários ----------
module rr(w, h, r) { translate([r, r]) offset(r = r) square([w - 2*r, h - 2*r]); }
module rbox(w, h, d, r) { linear_extrude(d) rr(w, h, r); }
module tubo(ro, ri, h) { difference() { cylinder(r = ro, h = h); translate([0, 0, -e]) cylinder(r = ri, h = h + 2*e); } }
module rasgo(a, b, w) { hull() { translate(a) circle(d = w); translate(b) circle(d = w); } }
// par de rasgos para abraçadeira (enforca-gato)
module rasgos_abr(c, ang = 0, vao = 7) {
    translate(c) rotate(ang) for (s = [-1, 1]) translate([s*vao/2, 0]) square([2.2, 4.2], center = true);
}

module olho(l = 16) {   // o logo: um olho
    difference() {
        intersection() { translate([0, -l*0.5]) circle(r = l*0.8); translate([0, l*0.5]) circle(r = l*0.8); }
        intersection() { translate([0, -l*0.5]) circle(r = l*0.8 - 1.3); translate([0, l*0.5]) circle(r = l*0.8 - 1.3); }
    }
    circle(r = l*0.19);
}

// ======================================================================
// BASE
// ======================================================================
module base() {
    difference() {
        union() {
            difference() {
                rbox(out_w, out_h, base_d, raio);
                translate([parede, parede, fundo]) rbox(in_w, in_h, base_d, raio - parede);
            }
            // ganchos da bandeja: colar de apoio, haste e cabeça
            for (k = ganchos) translate([bx0 + k[0], by0 + k[1], fundo - e]) {
                cylinder(d = 10, h = bj_apoio + e);
                cylinder(d = 5, h = bj_apoio + bj_t + 0.4 + e);
                translate([0, 0, bj_apoio + bj_t + 0.4]) cylinder(d1 = 8, d2 = 9, h = 2);
            }
            // apoio do parafuso-trava
            translate([bx0 + trava[0], by0 + trava[1], fundo - e]) cylinder(d = 8, h = bj_apoio + e);
            // apoios extras (a bandeja não flexiona)
            for (p = [[bj_w/2, 10], [bj_w/2, ext_y + 20]]) translate([bx0 + p[0], by0 + p[1], fundo - e]) cylinder(d = 8, h = bj_apoio + e);
            // ressaltos dos parafusos da tampa (embaixo)
            for (x = paraf_x) hull() {
                translate([x, parede - e, z_paraf]) rotate([-90, 0, 0]) cylinder(d = 8, h = 12);
                translate([x - 4, parede - e, fundo]) cube([8, 6, e]);
            }
        }
        // parafusos da parede (cabeça chata, 4 mm, com bucha 6)
        for (p = parafusos_parede) translate([p[0], p[1], -e]) {
            cylinder(d = 4.5, h = fundo + 1);
            translate([0, 0, fundo - 2.2]) cylinder(d1 = 4.5, d2 = 9, h = 2.2 + 2*e);
        }
        // prensa-cabos no fundo da caixa (destacáveis)
        for (i = [0 : pg_n - 1]) translate([cx + (i - (pg_n - 1)/2)*pg_passo, -e, pg_z])
            rotate([-90, 0, 0]) cylinder(d = pg_furo, h = parede - membrana + e);
        // passagem pela parede (destacável), para fio que sai de dentro do muro
        for (s = [-1, 1]) translate([cx + s*(in_w/2 - 12), parede + 8, -e]) cylinder(d = 14, h = fundo - membrana + e);
        // dreno de condensação
        translate([cx + pg_passo*(pg_n/2), -e, fundo + 1.6]) rotate([-90, 0, 0]) cylinder(d = 2.2, h = parede + 2*e);
        // pilotos dos parafusos da tampa
        for (x = paraf_x) translate([x, -e, z_paraf]) rotate([-90, 0, 0]) cylinder(d = piloto, h = parede + 12);
        // pilotos da trava da bandeja (sem furar o fundo)
        translate([bx0 + trava[0], by0 + trava[1], 0.6]) cylinder(d = piloto, h = 10);
        // canal do encaixe de pressão da tampa (em cima)
        translate([cx - 30, out_h + 0.2, z_paraf]) rotate([0, 90, 0]) cylinder(r = 0.9, h = 60, $fn = 16);
        // chanfro na borda da frente, para a tampa entrar fácil
        translate([0, 0, base_d - 0.8]) difference() {
            translate([-1, -1, 0]) cube([out_w + 2, out_h + 2, 2]);
            hull() {
                linear_extrude(e) rr(out_w, out_h, raio);
                translate([0.8, 0.8, 0]) linear_extrude(2) rr(out_w - 1.6, out_h - 1.6, raio - 0.8);
            }
        }
        // nome de cada prensa-cabo, por fora, embaixo
        for (i = [0 : pg_n - 1]) translate([cx + (i - (pg_n - 1)/2)*pg_passo, 0.6, pg_z + 11])
            rotate([90, 0, 0]) linear_extrude(1) text(rotulos_pg[i], size = 3.2, font = fonte, halign = "center", valign = "center");
    }
}

// ======================================================================
// BANDEJA
// ======================================================================
module cantoneiras(l, w, h, f = 0.4) {   // 4 cantoneiras em L que seguram uma placa pelas quinas
    for (sx = [0, 1], sy = [0, 1]) translate([sx*(l + 2*f), sy*(w + 2*f)]) mirror([sx, 0]) mirror([0, sy])
        translate([-1.6, -1.6, 0]) difference() {
            cube([7, 7, h]);
            translate([1.6, 1.6, 2]) cube([7, 7, h]);
        }
}
module suporte_vertical(l, alto = 7, fenda = 1.9) {   // pé com fenda para placa em pé
    difference() {
        translate([-l/2 - 3, -3, 0]) cube([l + 6, 6, alto]);
        translate([-l/2 - 0.3, -fenda/2, 1.5]) cube([l + 0.6, fenda, alto]);
        // vão para os pinos na borda de baixo
        translate([-l/2 + 2, -fenda/2 - 0.8, 1.5]) cube([l - 4, fenda + 1.6, alto]);
    }
}

module bandeja() {
    difference() {
        union() {
            rbox(bj_w, bj_h, bj_t, 4);
            // relé: 4 pinos
            translate([(bj_w - rele_l)/2, rele_y, 0]) for (x = [rele_furo, rele_l - rele_furo], y = [rele_furo, rele_w - rele_furo])
                translate([x, y, bj_t - e]) cylinder(d = 6, h = standoff + e);
            // extensão do ESP32: 4 pinos
            translate([(bj_w - ext_l)/2, ext_y, 0]) for (x = [ext_furo, ext_l - ext_furo], y = [ext_furo, ext_w - ext_furo])
                translate([x, y, bj_t - e]) cylinder(d = 6.5, h = standoff + e);
            // regulador em pé na esquerda da faixa baixa
            translate([canal + 2, baixa_y + 2, bj_t - e]) cantoneiras(reg_w, reg_l, 5);
            // conectores Wago: um encaixe para cada
            for (w = wagos) translate([w[0] - 1.4, w[1] - 1.4, bj_t - e]) difference() {
                cube([w[2][0] + 2.8, w[2][1] + 2.8, 2.2]);
                translate([1.4, 1.4, -e]) cube([w[2][0] + 0.2, w[2][1] + 0.2, 3]);
                // abertura na ponta por onde entram os fios
                translate([2, w[4] > 0 ? w[2][1] : -2, 1]) cube([w[2][0] - 1.2, 4.4, 3]);
            }
            // pente dos canais laterais
            for (s = [0, 1], y = [6 : 16 : baixa_y + baixa_h - 4]) translate([s ? bj_w - canal - 2.4 : canal, y, bj_t - e]) {
                cube([2.4, 6, 9]);
                translate([s ? -1.6 : 0, 0, 7.4]) cube([4, 6, 1.6]);
            }
        }
        // fechaduras dos ganchos: furo largo embaixo, rasgo estreito em cima
        for (k = ganchos) translate([k[0], k[1], -e]) linear_extrude(bj_t + 1) {
            translate([0, -desliza]) circle(d = 10);
            rasgo([0, -desliza], [0, 0], 5.5);
        }
        // trava
        translate([trava[0], trava[1], -e]) cylinder(d = 3.4, h = bj_t + 1);
        // pilotos dos pinos
        translate([(bj_w - rele_l)/2, rele_y, 0]) for (x = [rele_furo, rele_l - rele_furo], y = [rele_furo, rele_w - rele_furo])
            translate([x, y, 1]) cylinder(d = piloto, h = 10);
        translate([(bj_w - ext_l)/2, ext_y, 0]) for (x = [ext_furo, ext_l - ext_furo], y = [ext_furo, ext_w - ext_furo])
            translate([x, y, 1]) cylinder(d = piloto, h = 10);
        // abraçadeiras: canais, regulador, Wago, saída dos relés e área do ESP32
        linear_extrude(bj_t * 3, center = true) {
            for (s = [0, 1], y = [14 : 32 : baixa_y + baixa_h]) rasgos_abr([s ? bj_w - canal/2 + 1 : canal/2 - 1, y], 90, 6);
            rasgos_abr([canal + 2 + reg_w/2, baixa_y + 2 + reg_l/2], 0, reg_w + 6);
            for (w = wagos) rasgos_abr([w[0] + w[2][0]/2, w[1] + w[2][1]/2], 0, w[2][0] + 5);
            for (s = [-1, 1]) rasgos_abr([bj_w/2 + s*22, ext_y - 2.5], 0, 7);
        }
        // nomes em baixo-relevo
        translate([0, 0, bj_t - 0.6]) linear_extrude(1) {
            for (w = wagos) translate([w[0] + w[2][0]/2, w[1] - 3.2]) text(w[3], size = 2.8, font = fonte, halign = "center", valign = "center");
            translate([canal + 2 + reg_w/2, baixa_y + 2 + reg_l/2]) rotate(90) text("12V>5V", size = 2.6, font = fonte, halign = "center", valign = "center");
        }
    }
}
// conectores Wago: [x, y, tamanho, nome] na bandeja
wago_x0 = canal + 2 + reg_w + 8;
// o último número diz de que lado entram os fios: 1 em cima, -1 embaixo
wagos = [[wago_x0, baixa_y + 27, wago3, "12V", 1],
         [wago_x0 + wago3[0] + 4, baixa_y + 27, wago5, "5V", 1],
         [wago_x0 + 6, baixa_y + 4, wago5, "GND", -1]];

// ======================================================================
// TAMPA (modelada no lugar; para imprimir, vira de cara para baixo)
// ======================================================================
fal_c   = [cx, parede + fal_y];
// [x, y, comprimento, altura da placa, cor da prévia]
suportes_tampa = [[cx - fal_d/2 - 16, parede + fal_y - 4, amp_l, amp_h, "#6a2c91"],
                  [cx + fal_d/2 + 13, parede + fal_y - 4, rx_l, rx_h, "#1e7a3c"],
                  [cx + fal_d/2 + 21, parede + fal_y - 4, tx_l, tx_h, "#1e7a3c"]];
mic_c   = [cx + mic_x, parede + mic_y];
botoes_c = [for (y = botoes_y) [cx + botao_x, parede + y]];
logo_c  = [cx, parede + 40];

module grade(r, fenda = 1.8, passo = 3.6) {
    // fendas horizontais em círculo, inclinadas: por fora ficam mais baixas que por dentro,
    // então a chuva que bate de frente não entra direto
    n = floor((r - fenda/2) / passo);
    for (i = [-n : n]) {
        y = i*passo;
        meia = sqrt(max(r*r - y*y, 0)) - fenda/2;
        if (meia > 1)
            multmatrix([[1, 0, 0, 0], [0, 1, -0.6, 0.6*(z_face - 1)], [0, 0, 1, 0], [0, 0, 0, 1]])
                translate([0, y, z_face - 1]) linear_extrude(face_t + 2) rasgo([-meia, 0], [meia, 0], fenda);
    }
}

module tampa() {
    difference() {
        union() {
            // frente com chanfro na borda
            translate([-g, -g, z_face]) hull() {
                linear_extrude(face_t - chanfro) rr(tw, th, tr);
                translate([chanfro, chanfro, 0]) linear_extrude(face_t) rr(tw - 2*chanfro, th - 2*chanfro, tr - chanfro);
            }
            // saia que abraça a base
            translate([0, 0, z_face - saia_l]) difference() {
                translate([-g, -g, 0]) rbox(tw, th, saia_l + e, tr);
                translate([-folga, -folga, -e]) rbox(out_w + 2*folga, out_h + 2*folga, saia_l + 1, raio + folga);
            }
            // lingueta interna (labirinto)
            translate([0, 0, z_face - lingua_l]) difference() {
                translate([parede + folga, parede + folga, 0]) rbox(in_w - 2*folga, in_h - 2*folga, lingua_l + e, raio - parede);
                translate([parede + folga + lingua_t, parede + folga + lingua_t, -e]) rbox(in_w - 2*folga - 2*lingua_t, in_h - 2*folga - 2*lingua_t, lingua_l + 1, 2);
            }
            // conta do encaixe de pressão (em cima, por dentro da saia)
            translate([cx - 25, out_h + folga, z_paraf]) rotate([0, 90, 0]) cylinder(r = 0.7, h = 50, $fn = 16);
            // alto-falante: anel de assento e 3 torres para o anel de fixação
            translate([fal_c[0], fal_c[1], z_face - fal_borda - 0.5]) tubo(fal_d/2 + 1.6, fal_d/2 + 0.4, fal_borda + 0.5 + e);
            for (a = [90, 210, 330]) translate([fal_c[0] + (fal_d/2 + 5)*cos(a), fal_c[1] + (fal_d/2 + 5)*sin(a), z_face - 4]) cylinder(d = 7, h = 4 + e);
            // microfone: moldura da plaquinha e anel de vedação
            translate([mic_c[0], mic_c[1], z_face - 2.5]) {
                linear_extrude(2.5 + e) difference() { square(mic_lado + 0.4 + 2.4, center = true); square(mic_lado + 0.4, center = true); }
                tubo(2.5, 1.2, 2.5 + e);
            }
            // botão: reforço em volta do furo
            for (b = botoes_c) translate([b[0], b[1], z_face - 2]) cylinder(d = botao_d + 12, h = 2 + e);
            // amplificador e rádio 433 MHz: placas em pé, presas na tampa dos lados do alto-falante
            for (h = suportes_tampa) translate([h[0], h[1], z_face + e]) mirror([0, 0, 1]) rotate(90) suporte_vertical(h[2]);
            // olhais para abraçadeira (prender o chicote de fios na tampa)
            for (p = [[cx - 30, parede + 130], [cx + 30, parede + 105], [cx + 30, parede + 130]]) translate([p[0], p[1], z_face - 5]) difference() {
                translate([-4, -3, 0]) cube([8, 6, 5 + e]);
                translate([-5, -2, 1.2]) cube([10, 4, 2]);
            }
        }
        // grade do alto-falante
        translate([fal_c[0], fal_c[1], 0]) grade(fal_d/2 - 2.5);
        // microfone: furo fino com boca chanfrada
        translate([mic_c[0], mic_c[1], z_face - 3]) cylinder(d = 1.4, h = face_t + 4, $fn = 24);
        translate([mic_c[0], mic_c[1], z_frente - 0.8]) cylinder(d1 = 1.4, d2 = 3.2, h = 0.8 + e, $fn = 32);
        // botão
        for (b = botoes_c) translate([b[0], b[1], z_face - 3]) cylinder(d = botao_d + 0.3, h = face_t + 4);
        // rebaixo da etiqueta de cada casa
        for (b = botoes_c) translate([b[0] + botao_d/2 + 6, b[1] - etiqueta[1]/2, z_frente - 0.8]) linear_extrude(1) rr(etiqueta[0], etiqueta[1], 2);
        // furos das torres do anel
        for (a = [90, 210, 330]) translate([fal_c[0] + (fal_d/2 + 5)*cos(a), fal_c[1] + (fal_d/2 + 5)*sin(a), z_face - 4.1]) cylinder(d = piloto, h = 5.5);
        // aberturas da lingueta onde ficam os parafusos de baixo
        for (x = paraf_x) translate([x - 6, parede - 1, z_face - lingua_l - 1]) cube([12, 16, lingua_l + 1 - e]);
        // furos dos parafusos de baixo, na saia
        for (x = paraf_x) translate([x, -g - 1, z_paraf]) rotate([-90, 0, 0]) cylinder(d = 3.4, h = g + 2);
        // desenhos em baixo-relevo na frente
        translate([0, 0, z_frente - 0.6]) linear_extrude(1) {
            translate(fal_c) difference() { circle(r = fal_d/2 + 2.5); circle(r = fal_d/2 + 1.5); }
            for (b = botoes_c) translate(b) difference() { circle(r = botao_d/2 + 4.5); circle(r = botao_d/2 + 3.5); }
            translate(mic_c) difference() { circle(r = 3.6); circle(r = 2.8); }
            translate(logo_c) {
                translate([-13, 0]) olho(12);
                translate([-3, 0]) text(logo, size = 8, font = fonte, valign = "center");
            }
            // linha de acabamento
            translate([cx - 22, parede + 27]) square([44, 0.8]);
        }
    }
}

// ======================================================================
// ANEL DO ALTO-FALANTE
// ======================================================================
module anel() {
    ri = fal_d/2 - 2.5;
    difference() {
        union() {
            cylinder(r = fal_d/2 + 9.5, h = 2);
            tubo(ri + 3, ri, 2 + 4 - fal_borda);   // ressalto que aperta a borda
        }
        translate([0, 0, -e]) cylinder(r = ri, h = 10);
        for (a = [90, 210, 330]) translate([(fal_d/2 + 5)*cos(a), (fal_d/2 + 5)*sin(a), -e]) cylinder(d = 3.4, h = 10);
        // passagem dos terminais do alto-falante
        translate([-5, -fal_d/2 - 10, -e]) cube([10, fal_d/2 + 10 - ri + 1, 10]);
    }
}

// ======================================================================
// CAPA DE CHUVA (fica acima da caixa, presa na parede)
// ======================================================================
capa_w = tw + 2*(1 + 2.5);
module capa() {
    y0 = out_h + g + 2 + 6*z_frente/(z_frente + capa_aba);   // o telhado inclinado passa 2 mm acima da tampa
    t = 2.5;
    translate([cx - capa_w/2, 0, 0]) {
        difference() {
            union() {
                // placa de fixação
                translate([0, y0, 0]) rbox(capa_w, capa_placa, t, 4);
                // telhado, caindo para a frente
                hull() {
                    translate([0, y0, 0]) cube([capa_w, t, e]);
                    translate([0, y0 - 6, z_frente + capa_aba]) cube([capa_w, t, e]);
                }
                // abas laterais
                for (x = [0, capa_w - t]) translate([x, 0, 0]) hull() {
                    translate([0, y0 - capa_lado, 0]) cube([t, capa_lado + t, e]);
                    translate([0, y0 - 6, z_frente + capa_aba - e]) cube([t, t, e]);
                    translate([0, y0 - capa_lado*0.35, z_frente + 4]) cube([t, e, e]);
                }
                // reforço entre a placa e o telhado
                for (x = [capa_w*0.25, capa_w*0.75]) translate([x - 1.2, y0, 0]) hull() {
                    cube([2.4, capa_placa*0.6, e]);
                    cube([2.4, e, 14]);
                }
            }
            for (x = [capa_w*0.25, capa_w*0.75]) translate([x + (x < capa_w/2 ? -12 : 12), y0 + capa_placa*0.55, -e]) {
                cylinder(d = 4.5, h = t + 1);
                translate([0, 0, t - 2.2 + e]) cylinder(d1 = 4.5, d2 = 9, h = 2.2);
            }
            // pingadeira na ponta do telhado
            translate([-1, y0 - 6 - 1, z_frente + capa_aba - 3]) rotate([0, 90, 0]) cylinder(r = 0.8, h = capa_w + 2, $fn = 12);
        }
    }
}

// ======================================================================
// PEÇAS DE MENTIRA (só para as prévias)
// ======================================================================
module pcb(l, w, cor = "#1f4fa8") color(cor) cube([l, w, 1.6]);
module mock_rele() {
    pcb(rele_l, rele_w, "#1d5fbf");
    for (i = [0 : 3]) {
        color("#2a6fd6") translate([6 + i*17.5, 14, 1.6]) cube([15.5, 19, 15.3]);
        color("#2e9d4f") translate([5 + i*17.5, -0.5, 1.6]) cube([16, 8, 10]);
        color("#c9c9c9") for (j = [0 : 2]) translate([7.5 + i*17.5 + j*5, 3.5, 11.6]) cylinder(d = 3, h = 0.6, $fn = 16);
    }
    color("#222") translate([rele_l/2 - 8, rele_w - 4, 1.6]) cube([16, 2.6, 8.5]);
}
module mock_ext() {
    pcb(ext_l, ext_w, "#203a8c");
    // trincas de pinos nas laterais
    for (y = [2, ext_w - 9.6]) color("#151515") translate([7, y, 1.6]) cube([69, 7.6, 2.5]);
    for (y = [2, ext_w - 9.6], i = [0 : 2]) color(["#e8c21a", "#d33", "#111"][i]) translate([7, y + i*2.54 + 0.4, 4.1]) cube([69, 1.7, 6]);
    // soquete fêmea
    for (y = [ext_w/2 - 12.7, ext_w/2 + 10.2]) color("#111") translate([(ext_l - 56)/2, y, 1.6]) cube([56, 2.5, 8.5]);
    // ESP32-S3 encaixado
    translate([(ext_l - 69)/2, (ext_w - 25.5)/2, 10.1]) {
        pcb(69, 25.5, "#151515");
        color("#c0c4c8") translate([69 - 25.5, 3, 1.6]) cube([18, 19.5, 3.2]);
        color("#888") for (y = [4, 15]) translate([-1, y, 1.6]) cube([7.5, 9, 3.2]);
    }
    color("#111") translate([-1, ext_w - 14, 1.6]) cube([14, 9, 11]);   // entrada DC (não usada)
}
module mock_reg() {
    pcb(reg_w, reg_l, "#1d5fbf");
    color("#333") translate([3, 13, 1.6]) cube([12, 12, 7]);
    color("#2a2a2a") for (y = [5, 34]) translate([10.5, y, 1.6]) cylinder(d = 8, h = 11, $fn = 24);
    color("#3b6fe0") translate([14, 27, 1.6]) cube([5, 9, 10]);
}
module mock_wago(n) {   // largura em X, fios entram por Y
    color("#e8eef0", 0.85) cube([n*6 - 0.1, 18.6, wago_alt - 1]);
    color("#f28c28") for (i = [0 : n - 1]) translate([0.8 + i*6, 6, wago_alt - 1]) cube([4.4, 12, 1.4]);
}
module mock_vertical(l, h, cor) color(cor) translate([-0.8, -l/2, -1.5 - h]) cube([1.6, l, h]);
module mock_falante() {
    color("#1a1a1a") tubo(fal_d/2, fal_d/2 - 3, fal_borda);
    color("#2b2b2b") translate([0, 0, -fal_prof + fal_borda]) cylinder(d1 = fal_d*0.5, d2 = fal_d - 4, h = fal_prof - fal_borda);
    color("#888") translate([0, 0, -fal_prof]) cylinder(d = fal_d*0.55, h = 7);
}
module mock_botao() {
    color("#d8dadc") translate([0, 0, face_t]) cylinder(d1 = botao_d + 3, d2 = botao_d + 2, h = 1.6);
    color("#eef0f2") translate([0, 0, face_t + 1.6]) cylinder(d = botao_d - 4, h = 0.6);
    color("#b8babc") translate([0, 0, -botao_prof]) cylinder(d = botao_d - 0.5, h = botao_prof + face_t);
    color("#9a9c9e") translate([0, 0, -6]) cylinder(d = botao_d + 6, h = 4, $fn = 6);
}
module mock_mic() {
    color("#5b2a86") translate([-mic_lado/2, -mic_lado/2, -1.6]) cube([mic_lado, mic_lado, 1.6]);
    color("#111") translate([-mic_lado/2 + 1, -mic_lado/2, -1.6 - mic_prof + 3]) cube([mic_lado - 2, 5, mic_prof - 3]);
}
module mock_prensa() {
    color("#202020") {
        rotate([90, 0, 0]) cylinder(d = 15/cos(30), h = 5, $fn = 6);
        translate([0, -5, 0]) rotate([90, 0, 0]) cylinder(d1 = 14, d2 = 10, h = 10);
        translate([0, parede, 0]) rotate([-90, 0, 0]) cylinder(d = 15/cos(30), h = 4, $fn = 6);
    }
}

module componentes_base() {
    for (i = [0 : pg_n - 1]) translate([cx + (i - (pg_n - 1)/2)*pg_passo, 0, pg_z]) mock_prensa();
}
module componentes_bandeja() {
    z = bj_t + standoff;
    translate([(bj_w - rele_l)/2, rele_y, z]) mock_rele();
    translate([(bj_w - ext_l)/2, ext_y, z]) mock_ext();
    translate([canal + 2 + 0.4, baixa_y + 2 + 0.4, bj_t + 2]) mock_reg();
    for (w = wagos) translate([w[0], w[1], bj_t + 1]) mock_wago(w[2][0] > 20 ? 5 : 3);
}
module componentes_tampa() {
    translate([fal_c[0], fal_c[1], z_face - fal_borda]) mock_falante();
    for (b = botoes_c) translate([b[0], b[1], z_face]) mock_botao();
    for (i = [0 : len(botoes_c) - 1]) translate([botoes_c[i][0] + botao_d/2 + 6.3, botoes_c[i][1] - etiqueta[1]/2 + 0.3, z_frente - 0.75]) {
        color("#fafafa") linear_extrude(0.5) rr(etiqueta[0] - 0.6, etiqueta[1] - 0.6, 1.7);
        color("#222") translate([etiqueta[0]/2, etiqueta[1]/2 - 0.3, 0.5]) linear_extrude(0.1) text(str("CASA ", i + 1), size = 5, font = fonte, halign = "center", valign = "center");
    }
    translate([mic_c[0], mic_c[1], z_face - 2]) mock_mic();
    translate([fal_c[0], fal_c[1], z_face - 6]) color("#e9e9e9") anel();
    for (h = suportes_tampa) translate([h[0], h[1], z_face]) mock_vertical(h[2], h[3], h[4]);
}

// fios de exemplo, só para mostrar por onde passam (coordenadas da base)
module fio(pts, cor, d = 2) color(cor) for (i = [0 : len(pts) - 2]) hull() { translate(pts[i]) sphere(d = d, $fn = 10); translate(pts[i + 1]) sphere(d = d, $fn = 10); }
module fios() {
    gx = function(i) cx + (i - (pg_n - 1)/2)*pg_passo;
    zt = bz0 + bj_t;                       // face de cima da bandeja
    rx = bx0 + (bj_w - rele_l)/2;          // relé, canto
    ry = by0 + rele_y;
    // 12 V da fonte: prensa-cabo 1, canal da esquerda, regulador e Wago de 12 V
    fio([[gx(0) - 1.5, -8, pg_z], [gx(0) - 1.5, parede + 6, pg_z], [bx0 + 6, parede + 12, zt + 5], [bx0 + 6, by0 + baixa_y + 30, zt + 5],
         [bx0 + wagos[0][0] + 3, by0 + baixa_y + 30, zt + 5], [bx0 + wagos[0][0] + 3, by0 + wagos[0][1] + 20, zt + 4]], "#d42a2a");
    fio([[gx(0) + 1.5, -8, pg_z], [gx(0) + 1.5, parede + 6, pg_z], [bx0 + 8.5, parede + 12, zt + 3], [bx0 + 8.5, by0 + baixa_y + 2, zt + 3],
         [bx0 + wagos[2][0] + 3, by0 + baixa_y + 2, zt + 3], [bx0 + wagos[2][0] + 3, by0 + wagos[2][1] + 2, zt + 3]], "#1a1a1a");
    // fechadura e garagens: borne do relé direto para o prensa-cabo de baixo
    for (i = [0 : 2]) for (j = [0, 1]) fio([[rx + 8.5 + i*17.5 + j*5, ry + 1, zt + standoff + 8], [rx + 8.5 + i*17.5 + j*5, ry - 4, zt + standoff + 6],
         [gx(i + 1) - 1.5 + j*3, parede + 6, pg_z], [gx(i + 1) - 1.5 + j*3, -8, pg_z]], ["#f0c419", "#2f7de1", "#27ae60"][i]);
    // sinais do ESP32 para as entradas do relé, pelo canal da direita
    ex1 = bx0 + (bj_w + ext_l)/2;
    for (k = [0 : 3]) fio([[ex1 - 12 - k*2.6, by0 + ext_y + 5, zt + standoff + 4], [ex1 - 12 - k*2.6, by0 + ext_y - 2, zt + 7],
         [bx0 + bj_w - 7 + k*1.6, by0 + ext_y - 2, zt + 7], [bx0 + bj_w - 7 + k*1.6, ry + rele_w + 2, zt + 7],
         [rx + rele_l/2 + 6 - k*2.6, ry + rele_w + 2, zt + 7], [rx + rele_l/2 + 6 - k*2.6, ry + rele_w - 2, zt + standoff + 6]],
         ["#e67e22", "#9b59b6", "#16a085", "#7f8c8d"][k], 1.4);
}

// ======================================================================
// CORES E VISTAS
// ======================================================================
cor_base  = "#4a5058";
cor_tampa = "#c4c8cc";
cor_bj    = "#f2a33a";
cor_capa  = "#4a5058";

module montagem(ex = 0, com_tampa = true, com_capa = true) {
    color(cor_base) base();
    componentes_base();
    if (ex == 0) fios();
    translate([bx0, by0, bz0 + ex*0.6]) { color(cor_bj) bandeja(); componentes_bandeja(); }
    if (com_tampa) translate([0, 0, ex*2]) { color(cor_tampa) tampa(); componentes_tampa(); }
    if (com_capa) translate([0, ex*0.5, ex*2.4]) color(cor_capa) capa();
}

// nas prévias a caixa fica em pé, de frente para quem olha (-Y)
module em_pe() rotate([90, 0, 0]) children();
if (peca == "montagem") em_pe() montagem();
else if (peca == "explodida") em_pe() montagem(ex = 60);
else if (peca == "interior") em_pe() montagem(com_tampa = false, com_capa = false);
else if (peca == "tampa_dentro") em_pe() { color(cor_tampa) tampa(); componentes_tampa(); }
else if (peca == "base") base();
else if (peca == "bandeja") bandeja();
else if (peca == "tampa") translate([0, 0, z_frente]) rotate([180, 0, 0]) tampa();
else if (peca == "anel") anel();
else if (peca == "capa") capa();
