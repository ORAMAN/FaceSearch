#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK ID Calibrator — настольное приложение для построения опорных точек
«ID пользователя → дата регистрации → год» и приблизительных диапазонов ID.

Зависимости: только стандартная библиотека Python 3.10+.
Запуск: python vk_date_ranges_app.py

ВНИМАНИЕ: используются недокументированные методы restore.getSettings и
restore.init. VK может изменить или ограничить их работу в любой момент.
Полученные диапазоны являются оценкой и зависят от предположения, что дата
регистрации в целом возрастает вместе с числовым ID.
"""

from __future__ import annotations

import csv
import json
import base64
import gzip
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from facesearch.settings import load_module_settings, save_module_settings

_REQUIRED_CAPTCHA_HELPER_BUILD = "2026-10-31-browser-bound-init-2"
# Встроенная резервная копия helper-модуля: приложение остаётся работоспособным,
# даже если пользователь случайно распаковал/скопировал только основной .py файл.
_Captcha_HELPER_B85 = r"""ABzY8AlT(<0{`7z{ca;ia{pgX(WZf$VN8ysb<S|OM;ktWz>Xd7!n=0<&?&@-8cJhP9GWwfR?!jgdTqyneRgadkl=vx;gURntX=JLKm7{iE96O1Roy+)J<~lLO6%B&aOdUB^k;Q-b#?Wxs-JykbsUdYx5B|{FgRetqhvQ4JiqcRYyM4>b))TYu+w7Wq}O~A->lSXwb@^0Pi7Bik7rNjpU=+V-^cSWXZP8wyQ6479Ph7Q?fXZEqi|<8VY554hx2dP{Nwqp`M+QeNcX|p?ApFRN?!GcNq5)xR#s;JF~2$c5euDvHM=*zH9MPs!tllX=KK@*?>A{3vmfUl!;4R6Q%svy*<iC%`2GWYJW<lh65-#|*$EVN2F*U3J@OcI;RH*9=jjUMIAila%pUMoKx03dO{vLW&p&}Cd~=y$GiDF4v!V%5;2He;U^Z>A_v2vHd}SvXK!2wUOM~w}%s+$w-hqZbg4Ub?C8NDCh_O|e@eDql$nsaPr$n7o`tPIpEnW@u5+3gok*D*o8PJetzK0#gACHs)pu`V=0@!K(>aDA<Hi23vv-`7$axjQ4r$ksF7X$K~2(sRU#}D52aEw=0R(e2N*6H-d$#@iWIxO5DMx%uJgF%$|Nf-^{m6bPNf9LA!?{$9r{#)<7#@eiQY3<Sr&GohB^Xtv6QFMre*^0)4?PfR#ljbETR+RE#91Y~}D3-tCqx5GI><@ckKakJEzMu4>(Y|~hrC*Y8Kj4iW`pIrT+>$N53Xl9lax?@smhWE~9P!uVQ6G|fLw^(p@*}*X=LY^755#Z!r}Gi?cx5~2G2reb=!}AR+)rGrwMCUR*tVbeEw&wYlZ`kTHL$A9I%~ef-i-!9YlXodCdVp)UA+Ch?QrDshq!%xG={2z8)2MuqCI-7Q<mO#gC+YzAh4tg<c#sRI~)bQ@J5>o9r*hJdyYAt(_rzqhwmK^^1&li(}o7QO67H4b}~BR?ZUsqFxiFS@Os<PFc`R4uX705g8>%=ZHEMbQ)hn6dRadF!x8ni$IvDuLWA`%Ta2vZ$K5b&zv1^|(I5PST1Ej3YBzA9i47tP{~Fxr2E&BCPX7R#Wz}F?G2?j94+ndgFDp;vx^nIH(I^_Vpu9LvKaTt`4#e2?!ohYYiT0p>2Y!DXv~ZAsLXaDVF@#CnW@|KNKrr@uW#qyhPsLth>uXu7MgdHa0i~14!Z;kni9dkOQ5_9piF%d?IGODQP%ZG$K2*vmnTJoYTh|E-<v~(R<T~-V+YREFw;u*ZzPL`$Ff4;@wQ?-4f&JiL`p{>5Rc9}?*^7EDN>4C})>6HZ^xr}&dpOO3bP9QS?VM!KvGsbsSCpkCYo#a70DU6~`!FwZB-9%AleI@3V9rCa-wQecGFoRJsPFFtF7RKA{nn3zly4g>f<-VK`27xuk`^BZ&Q`psxRm}GmIjc^Pe20S0{M&58-zNS(Lch&9a_+)Ahtgm2cx4VzNA&|5iXK^C4vUR{0_Pa)DyzOz-ZYyncb!(`X;SrxNu<cQ&<#eA$))zJe2g<6wwQ$SM3<2CH1H7bvh_hI-N<29m9)BO-L{hhDci}D_ymtoUBA4c;cyq9Cclu)r-5k!M+r$+uk5Z`cXH19Fi!l)#U$$q=FIB2$~LG)_CzNl`LxdM%pG`+_Mac4<vcQmqJ>l;l~$1qaTn`Kt`hUeX-WVQuzqTeH#Yq6MYe#!bp6@-hb~MHowV7^9RUxO3DUboF8Fv`JuyKQ;!;hOa-IpcN2Ht2dxOX(EA7ch3m+s&Np159b$hmg|{<`#zS|luE|NAxpAH-;{~}3+>dFz-6&3I8G%W4Tno3ogFSCF#!=wEyU~7aVig?fNK<igoasT1GCf$vxLp2o=2PW5uNS(bPE;UWJl=9gwcR8c#xJ*4S2zCYck%CP&Tn4Yd=6^kgl<S~sc*E_UtC+;6mxsu4+kz09M*zV@T2Gi?Zop(I|p_48+NH6|1XIqpia=i?g7D3DM+(ztZ(v8@@XMv9^@V!bque7j{bg}S0*6U@r2L9QG}X+sKx6ZZo9nr?a<#Dz&M6o$c;jiSP0T>^lDd0yNB6~jXDTMG0@kGhy5^d>l<sE6Xw#F;6~6LC;nDHnAB^H!f@+<z1o#)P@+`OJo&?|PimV*9b@Pp^&=m}2WbrWJ0=>%38)w_b$#dv%yi})Pn?V^IEA<lLj`y{LE<{_+Tp2{v^r_rLMHLO15z{1T!&5%VUZkz&g#~o8FAD<2;8htq0e?Q+L+o?OqJFE9UO&2r9^e;3k9VgOY-}N9hFz07()^Kn?fDnX3*f(1vXz$L0w68DE0xaw4`FW4qv>K{S1n_f|%p4GGU1uoZGV}?3Jr;HTAuR=j3UV_Ujnwvsb4@)P)I1`cs1)pq$FLUTRU3Vqv*uE2XhU99K;)L>POf%^VUZPGL4cQ?wkZ3?(Xq9CiF*D1;}fY~pD&b|$8>Fqc*y>FmK^J4Wr?byoKSn8Vwvz@bS5q7HXfz`j)mD^oO3C!mcf>>5vKojb#&Yx!|15;adw8y&H)R*K1$2kDirJkCVPLsS{@DdDWbB*Btbp@7aP+JgUfxpl!=M3)cV)>-%=-@q@R6uuY6hUxrsVsYIn;1wqvO>r)HDo(9?QUPSB7(z=<Xp4VaH-o^Ao(v!JTabYi6|X1@t8O)O3y9|rVI#Qh_G+`g7%eX-_<0SJ+QJf$iLfVy_TS-pD)it-py)|zsUWrk3g4Vfp(5D%`Lx@SWNhGO@E<!@-ma^a7qlO48%T{(UVJp@c3>8=)an4w*>_zr;z{^FXe@x)=i>%fOI4_nD4&+t5i2cBlVU+~8HQyk&EV+Tnb3#~d9V82Jy2oZjgmKTyTbRr)mq|kW<}Z}sgdcYKyZACOb^4Li#p?&W~M%_0pzMthedSaE$Rd{6paYDZ8FS-_r?P>>xkK)8WhP^$LHmVFqu8%cDd7>5fBY&j4M1IoBcaUFfpNV2L$ucA3;m^I^i}*3n1&r2Z8Es`3cM)&NR@vm5i@&6A3E~wx2{ESv1nm+Sa^7J5r=_MnoZzNHU>Wg!Yem5+G_PhC+aJ_KMU*C`vofkz^cm+Xc5*K%UebA@(|dMS{^T=YDR#$jzi^pCvM?q8=%TTp`E^f`;23b$J7;{`@m(;Pli=zsCZnMdL0SJ-JO4U&0nFo$;q6<ECUC{T%+CHgL$F@NrMA$xy}@!YKL;d-av8*I#|}m6lpDd_l7*lZzziL)U^N2?skd<6ALo_$f31_#Aq!+Dz4j6}x&1q&{u=kh^{qrtdRMtk#HBq8{OzNp+Km1R14mG4B(BX$Ss@(fbFqaFE5D*tsx58AXA6`mhn%3DWT=X482{YM?U7cnFdru<s+(JlN$Bw#xyxp_#I%eg02%0Fh8Q6Hn!8h|&b8%eT__KVhssVMZfHX7Ui!M&tGB>@GZgOyxY(SnIER`z+WFHuoAu;Da_FnJ|Q;CU?ebgfiyephS>M(&r@pP{~OvgMI5^G}`N+05s7=6JJhojE2Bau;@B1MyqZ<dC=X1q`?5b(>%8%;APQ<KZrYNEszUmx-k=oHZB^<5-Ad-EZHd74{yY<_-rfvvA0(wub3M5Tpb?|P!=T5^C|vYM!bCb%Du>E<c1_Ofr^#%f>AK4miPe2GDl!B$V(QW5V5oXb@?bEot|td?&G_clCpV%%7dXLub6r}LOV_;u9#f*kV_M#qd?iRPG*g&oJDC_<vhi)U7V+kWo6?_7?mgKg+X6s$m2;a88qww%^ls{$dYWLEMLf{g2en3L6xV%N>#EfEs<4Ds*fOL(%B9CZO}-Ut8J;wg*j+Y0P_`Osms$vTS#R(a_#t|eS=CUM%<b%+n5DCmUb*0^deV0<aVUS4UHfKlqxcLAO<9PT;rC-V#KvpVXj8JDWqOpiC*6EWN|0anzR*dA1T!jVb{~a?G`u0E!j_{z+7JQu93_e9W<blJ&&I|yRwu9+Y5qWXRGfI_QXWU?cSC1F)ZPDuf>nY;*I9|W@<R2H)v~7>MI(~s`XITq~{a+f%0i15AvWDWcfiGi5M?#!{nu)83;|Z+*0jacIw4eggxf89nq!4%nE2^5}oJ7O{7s(7Tt!b*6YU1qC*>42zG;tSuq@ghz>M}Fb$bcpM<o*Blfb)z(F+VN3bomNR~#4G^xyFQV^u)bkZ!!`ChZ+rLzep10;5fMPz0?VXy8-hiFST)J}&Zh7!oAXUk?LD->iFXeI_(ELriGoVr+UxTSQJ46|lNEM3_=zGZVo59^gm$gNhg44nQ3o4HISXMD9fv<0g$Rq<<+z7|hpG|pD`QyV2JJ<c%{ZRaS&pju*STT#@PRFy|0GNDsHPiuvewP~V<_;vwWNCnMUy!>b_pI{Vxy0)3h%0}iD)~P|1kp0}_`lW?^^=2!3*d@kn(Z)6%FzfLntyx41ZSyZ+F!rxN4vQtlSXI4pXMQravu&zNOJA;V!-mORdX~^uyg|>eB+`sJ`%@8QR_**L2{Ee~BO{@)1?IB32t@9{+@&i;o|_uh28-z?O0{ug<D8PJ%#4xJK<O7wP_;#bTT{B-R=-}TK?bu(+Mts4Eo)$bYw%Oq%GhRA2m4|Y8b5L2RZUD63_nW<>uT1ZU$Dp~Qrou4|3^xnPEy56w!}bKO=nMMp|P3Tp2|XFwalG&q2UGcOy?46G*Mv>k`-ZCx~dnKqn@Qnm0F|~vY=5F+LX~U-zbI3i(91^V{2BWBF&Sb#0tAuxM=K<3h-=r@s-Xp?Oc*`>wo3e;Hpi}23>8v{<gql?P9_UcEf6+uG$ZUP1KI$UKcsDZAiLAUxcmPe|mb|0_%HKdO(vF=o4AfZQ8>H$MorLRQ2<N^KDPLZE+ZMImQ94gsuwXE6=f!bA_Nb6cHH4GWLQaOaya&AMsFV&9V+)(c?nGID~kUk4daP`3ZXGB#s?Bn<j<U5Xvp?#0cR@fot$?%&QQ(MJgWgt-ajwz#k{O9iH{Txgg)@2SCO1?i^iP%oV;WWE4i$M$SkTJMwu!Q!X+(ev#v;f324(kkT6_Ka825j4aex%Cgq&XW8tJd3Op9VtXuoHy+OJ3S0w0QxNAt{)Y3f7<mFB)ZqsK_P8bSDxSeE8Y@;}^2N$Q(_C`A8W#M6im2n3;jl!(EfX%fm}?;jx*Xv5UC{?qLkf`>Ns275RW0_vrT=0M=Ew*Zavz4^Z1z|y+k@GJ0718Iszu;2P07uQy8)91&H69$*14zjHuJPJwDip0+<YU0eq3n)hhA5}FcrjpC-bCRNLq>FWEzg|9AjiC=;s_AtN5@r)Ql<gm>A@{0y0PlLYmeO1iXuIM2I1~DS%A(399=UtJ&K?ZoN~9w5tY5OtEizGZ9-QyX4g>$ogm5RfJ~737RUp(I5$KBu0oH2dClSWym<)``YYuZ6=48YlL(iKo98$P}tXm!Q+@Xc*TKmihnZ#%utekiC*L;PjE%XBF5`qAsYhm_#mymzP%H8i;u-5J=#o}S$%&z=<argDcL$AnVHK-Z^OglPU>?jFy9T9o1|^N0H(sXLDtMZtnzIGQbqn9gux*qL)*vBVYr>_IxV)o{^A-eoVx*Ghg<CL*48HG%s54&angQa4gSnY2)3zH+T=$+|0?q*#DTht_`a4zh`tsaTnELNgDr7T9uk!O2`?0;2SETqK6=JB_@kY;z2P*QG$ES%{*7=y{O4fX*(|oX9Cu*E7?aaryu+jl(zTye;;AwYDS37?4$6#1pk>6&@1w60DIAc3PM)z)RCi&>QE80B!A?I&qCx&6@<aL#l_aMh!G1!#xwx2v)#*>A4g<7mA@Y%&9AO}C<M8D{kO==Z0R6pE;f6qKj%mHmW#%cAgr1eaEvO`AF&heVc8YTNCY%3(fVB5HKodO%;Gw|?7yJWC0unp4vU0Im)*OQrEBB?a$a6zHhHq>VHv^BKcMw%%y;z@aa_`f^vY|L*FxTX}TNlo|wqcXL9l{^rD>-WAx*U_3S#)&es#rN?RZo$XiX<Cyw_YJ|J$#He);4igYVSy6N?#3v!w$ZN>8yJ&1U)9f_fq$jvYZ?~b!=~O8aj?M`wqHxj~1sB;MfydDs?3LV<6F8qS=`masvhsm5MtQRs?t*c&gx9g*a$oo01B`ZQ5UmwccAPv<WsRa){21!9}#7A;kzGhBzX6&=5;#l2l5}Zxv;74)Tr=daCpru37N48L}ex+@lgDeW10pzVP|QX5X3Oz?>DQ=ya7c-E3NH%sm0}mktAID3-@k2@#(5NHWhf>V<cH9~TU1Hl&$`NR*sORyP`}_UuA|ANr$8xW<L(IW;VToDibJ?gn?gYpt#-XB?XrwOZn(#41T%+hp0nm*r+1nrzhDLT3It^J(o14?eF1FO+IN%AsvmBE4PQ9^6(4$9Ci3;VPWj<6PPNHgn{e98|#HD#t&Pi1iX8>cd+CAFB>&)GY;>=Js1I)g*E;g+2<Yx6P$&vKv4Of|iLRHP!=)fYucl${dm<kG7pj1o?oeU;xHlbf#gi_CwrAycrIXm=`|q55gVXwFr!1aX#_$x6J5gI7dKQmmYlAS1jl}oGnP<7IQP26Qkf;;u2ZE{<e&F#I!2opNXYG0>*&g&&$m5K8(VF+jFRillHL&DV#V9YvBu@lBZte0u7{PF*zH^Yk?0cJMt`rnJKLzsn9C!><(M90Gos84hI(C_DdP_c$of`#pe2I)1}Y=kmzPRA1&6(MiUn4oI4olNZ*2De1)v$tM73;cjn$idsWy_i}+u{joMUQ&Wld^j%mKum-!H4@ptCmqONh1r4G@gBs}68DXyzi;ZXHerq!bw&F%Z11yG4*=TSE>KuvN;92^HDM5j<N1zJ&H*DF?QadvDVnj!7LrK7GGflDm+;#h;$VSW~Y$N634lxE)_?2LgMD1=hbZFYB?W53L*o+fJUM_agiQ!_SY$csfK^nwI3#IShD{QS*+w4Hmi4fMq~!%+zRNRF1V<0GiVNYj;aTet#YHV>Jj$uy^>%eY2q9+F>>h2+-qf^O4NdN!@0?CimkTJR(!RavxQpi5!nK!ELd2#X&Ar1=lSqMyRHb#LK@>2RuH^!IJp9DEyQ58_7E14FBPPJ!6GS4B8De{aTv8!_QoL6t$_CSbTyjD`%i*{!}KQ|)8!PK;9V4nKvVZ4glP8YgNi7rud_9&)+Mx4>WUeVwHmaemW8vj&+-FfwDo&hW_KNuBL-XvjS4?EUE=VI>aa<nbbgn!mA@(BVwVf%qK*1YZ<m#o}vCK{G|BiY_`*m)XjpWaTNAgp=7t?L|>`7eU$;X=kmXR5g<XB@3s*0~nsktuM@m=|yWOB!xKr=Y;^R^;mu~nFuk&4L4e>f<+vw#6?)lD11p19g6l+*2wW(E^MOgmT2U{8f2-MM=%_Bs3uNxuDtD(7Ev-iDyz4PsI-^}^)e6(=ma*Y3!?L_*SP$X=0Iua;Z`S%3qV#yki}Z$UuoFP6k6+v#0e(Lq@hGz3i<bLl)RTYEm*~BE`SFtNF->V$q8B!gXuyJ5(?SOpFT>J4|?E|6o1*>nyScDnMJSxcVe^(LOVLoER*x4bL3L4MyqJ8|E#Vg6(pspP&GWlX2lh9S=SI{*ncAT5L*rB^5IYlJBPQ0!ur1)kBcRizEmsC;uuy~_Np&=Y0wU(JsmBqU%IyFRpb*_tIkB#EN%$dDdm&d=pzEqC}DQ7uk}(HECh$XB9U{Ctg8;pHbh-tE}%S#Hizw!*BpweT)!(scPiD{WB?jll$mdhF^(35oXL<JW%YiC=QH|A51OO!baeg&A|XDd!}i;`z)p1Xk%h5W1Ki~QwCLtaK+{GE_K&vLoQBa>;)jD^+ZgbskVoC+L5^PS*tGrQ(E}%~)m7tX9$I#CY}+|bYE@g2M#9Mn`UUybP&?^wdmf9M8ab&D5$(Kfiy~I82UD2CQxUBT^f8@pMU1VhSFT-WMwLn&l|7-ja8GE<E21bpVCA~4laI^9!(?In>02pEM~imF=z0keg)(P-#Ria-6<GdpV^P&~lt{*0rTvPIN|sXfW``##9s(_=#jQR@c09)((Eyq&Z~N?fXxJ%f(R>8RhH!^Q@Gga^G>xD*(Im0*1<>fBge{zcMt+abr!Zm~{qaCIQ}oIy+H>4DuV25~KF&>!NdtEQ{CE2}<^M?*m5X$;_OUL&C-tRhSsB`6N0R=TfPVZinq+kWpvzIaG~P(1Q;tJ<k`9IpHpfgzi(1Y!MQ0os*-<FTpm1k#AL_QGLWO9kCdXXCunhc3pkI{G?mT+lT?t1013@u<Y-y(vh6s130=G=2vop+zsR<I0uQb2Od&q?VPHKLahQxnm8f*hMZ-Qzf^j<c*|L5aB^4uJ!S3n8=C0)TdI1u`IDzIj0Y(L<!EU+KVl6%>rm_^jfcpQ-o1mpZT2NrlX7r@VUK%>E0pntb<URq&tx?ype+XH)5f^`6E>_kcArpNbU7Ysad=!YOS2T9m(<Jmf$_zx1){{jRZNW9<rB?SD{tCXE?#xGcW<>_h*7K{9Po8w3b{c>U&ko!!P%^29+cr^pv=3;{z6A?n#F!m$R9(eF^dWVK^JY%8|_5K$0v_*k;YSod*7Ma82KF+@+X`_W8R#=##SP~}I%!nyVdo@$MQvBQUX+t2RXs;L#;kbgFUP;0qF|f8FsJ4lIcodgLZqB*2<h*4}UM>qfX!0&N`k9sf1RK8u_7VQDtk2|D(^5$zHlosBv8cB>zS;sqUO(p~WGv*sl=A?S|Ju8B>G^z~T-PcD1uJY>QhLy-ihAH)tJQpTF2R8RLVmuFcVuAk^aC4umD)L>qxoYKS~^Y`2FH4X2Y?a<(p?+Ehs&HZ%Pj(94T3|wKI2;8kGi|OR=0lHdiLw`%o`cIZnCJyu55h>N~8w@YJ_Mf(S&-*t_7pLb9R3Q_L;z!K@WE>>{q{H+3Uf$-^VxCpxD6<PhY1mFwE2gDYmFRS~5!~CM6`1OFj}5#V?}+VNO+hPq;y}#T-=Pfu-<7xrK+XBb-%T(6E6lz&A0x3<ur*m=2t~J}8&)W)L-LEiE~}PNU!*JFoIppm`lMAYf?+dfyMb#EPponumvnO*A+*p~ZA>6s#1W2_l_ZoLWgFXk^xxCl;dhgM>^imu=`1b}S7!oNzgF&Se|fQ8#67r=I1<A-5%$C*(5fa(<)8qP>=C+~AjjO^U5Hyy6^?Dm1bqa*ny$R5ddLs->;eXAs8tU~e44i*@)ZVG1qWu=4l@s^Egxn7)Pb=*8!Ys<~-k^cKIy$tNE@A}358?S&_abJ?_*xX+}CqdoTf-?L}5!t*b@qI0Be!Yc!}gCtU7LPNK;NZM%=7H#CYqS2+vrgidVk3NZiC<S}@Wn!#FrJ)BvX<_Iz0A)GG;MTMQRCoFY?@<#o+QfUhj(VP#tqBc7u`RY>U?%1P*K(!at3`=ik}fBr%_%dPeQbk4;HsRJ+_qF{pdPofn#<h|Kyn3z;;WAVZPN;}AYG}&I%k`kLZo4GJ|AHU9b07&aG11cbITH>CTVCKI!Ll;w%LueVj60ZQ*!f=8og)^#uC+Wd=8$3je#7HmW{)3t~rwh)3&w1II2nt_`Q@|$jOE*yOad8@6&6`PMA*6mGYzU%<#s`)vgAhj+d99==Zz;<;61jlUUE5#F%@nT4u3V&`S_6v6ywcA=a#cTwVex_f+`!XXRnJ9=A-I%A|(7U&Ag+(M8ByF0NAYMk12xDn(L7Z=L6uvuaqY44mNzp{5pSNR_QaxTDJL`Er}6`qH~7J+!g{fL0FM(n^h2Wm#+i&g@&=gQ~Dr8vqmORTb0p0%e%#9Je+Vmu1|Omt|;k-*XCg{H4uKPZD<m0a8&1rgZ_oHNB5HgQG!H(eKn?WMF2;xrVBz@UUHe*{Q>h!s^Y2{(^T3Wn=;ua@Re}caW%j|88<pGqRF?&jvqz)`%<tdAajiTQcq3_PE>#AP;{`U0GD2NK{sx;Cu7!AS=eRNrS2C5xIi_C#2Vo;^ipPA}oEd$R2x!R(Np-4ElF{)na6V*BtI<j|^3tW)5D%DZKgdyePGs6%hUC8K;=!xRY3FTr1Qj>7ne@bF|HH05)d|bzE}ASw%`Uv0}FID?&y&%@B8$-{lwXC?uMHN_W6L;{KeTp&!d05*H7LOMKi@E8s8~;f8f#dKr*NkiAtHqtbl(9VjKTr*0`mmjO!k`UgwyoG&Wk>TO=CrQJ{r=}?A>>@XnQD+Ttb!dlwthCC7ClS)kT%Y{jdSJ@XPtclC4z}3{X5pDKv{UUyJ7R7FDrEo*;AaRYdu(-hB5yBeXNx1&EEwg`&l--u;qLkKR6>Dvj6@@EXg_1B?y-`8`{e=sFW3j-s6lza?$x`6woWr^0LF^^xw;E~e0S+MGp60i3`rb_ASpA}>ZFakE3FoVuUNZ=B!8NDpLE<mys?!T-3mI?>1iBn&0Ws0<4tzp5pj-4`P%32TAQ9k*yF{85jogi@c`mYFb?2scq6qSQG!8~bb+e-f5OKqCHgOwj(rhiYR3zkg0$)HnZWX#{m0Zqr;1t#*@5jroKcz?!pCE$iN&eRPlQb9<t)-`QC8B;VoFi)PQf2bKRt_ZXi8o9!2sd*ROP6w~BAm}=oL@=|J5w=POvAdII4<2;g#{U}qgR@qWv<d<rK&1H*|anAVKj8L_>0&^J5|%r_AA!q4^PosMkAVbvff%G;nc)(08HkW{P8^0y;fFv5?8M~iKSjtQlLEcuUx9kKt-?DORYb7u(R!^Mx*P2acTW)ibpcZP~|G^qNPKHA*I3OG7mumdRMxJeDHRDVw>6~r3-Dlf8?QkG<;Nc5hg)w9$7(8rVV_9aARqh7oLjlO6BtYM-iFjPI~31c0cwBLBR?nQHYlB;sR(C9gqKfXr({XCTI%cmn?D0t+c;9L{b_pDvczCIMBx#gp|YAzA1Xi>PDN?Vj_(-8VqRx&9YYDB1ZiA1+QOg)a8;!s1}<Oua=K<2y-*N+ZXxmE(TD@Bla8Wb-aW|RJXHd9D!B9L}#wV=D3FUcnCBe237zUwfo|4afRp+;rAbAVIy)5!Ywxcl%~yN5fkYSVZ{utcDZI`4Kz6>BSfTlEwKz_ixV*&lTQrzMea|n_xx}->~ozq=SP8V8>DHOVCzd<3TT)Vc8vViDDd|zI|nF(#02arJ3=71>%U$*?^dMXR(PIP(JU>P8!D2_Fu9C5e;K>9rL6{X7jKSzV9uj&qHDh!)Og?><^4u;{Zec5C$*<p?MRCl#w0Ftenz=F+ow9t{{-I|aQ@o<!^1eAVnc=j3<`n|H4eMrGb~)>+l>)SL%M+;XP37h!R(I)VHXy$=hzG0T9G4C(P8voMgdYN={H(k=Lko%lsGA$yd`4Agt%VLt(#YCQV~qVs_Y6;vk$Ds2zYFgTcC{tZ`>N4r|vW&-yLyA@v&I9A5**73k3H@rpDFqvQ#VO+C_}4IvC&sBR`1yvqw$TYhnGm<<;uO_)mWlk%+n9$C->=J0(NNSxYZEXF<zUS-v7?o)PHUM^T#Y=hc8OS8VR|YRtwT4E7qrgu)E|Pt-Kz?txd7o!v~ylL^)F>+f7^80zBD+nN-;t&t08)nM0xZu1SSN0BcNcpzV?*`Rlla4R++2VOl&c5z&P;Jy(kTGC^0RCW09;R*IB4P)w8X6m#WP(`H>cRlf{uWvA4`hMOJviR-^E!#ydps-)pY;`(K?c1<mmVvny@b#nweSHxG_ESe;bEJ9>MlHN{1-1{-E38!sTWyE+sCd;u@&S)D*p>~(oy+T@(bO8*gR+V)0@*gew#(D1knVC^lo{^5083RF3jP$};0t-78dSUtF0KrZe^|tX^Yc`D)^fCY0dfBK*RH(liFjH5p%rUx!J8isKn5U6y^~?FpcpR(LI&<ygRP^%Vsbejm`wd!T}`9vbHOM)b#X;VeO%{XuBwLW)k17s8P0=G+tSKTxmBwyda#$qRJ>!jLS;&uPGQ3Sga?PG;EABD!iw*ex@|~kxdK&%Cq%!43{qaU5!@8f6n7R0Au|N_a%mpqlc66{pu>&L^0e6QS4JcM2=q?+*X58)mE;R0CD%83g~jY%FYG75$i0EG{|1uehKHg5ynTPjU%JQS^d;sM|0=|73lh_hNtw)30M7ygoNI$J5QdM-42as-+`3|*9QDyv*Q0vcVew{h$0{%g8uYG8-GQ3NYu4)FH81QPW?7g>Mzo33FeWWYj^PM4MM!LK*N?9p4z7-(VK7RLysqExyV*L|V8#HD#lM#`5WUQc6h%_Pz^Mx`$CTD#S;i&0oT`h+F6Ce)D0MlUx=}H25lY>t5K2sCxR$~R<=vm0M^%vkV+~1M@HV2aRb7r0K(8~-!EBi`l#p6Y(VbXhx#Vzcs)|~QVXF#{y%=asY+3Y^n6pxZtgdXQL4mSvi5TYa!Ld@MKoK?ePcG10mcWF`EQ#RQ-Hm9!kQJ~YGH=R*SCv!C<D#pP@2KDg>xXf&z#^g_rcp@kQ3vx^ye%&C15u;7<AG<r-eTFCqVRJT%P}<yz95#~QT(V4ir#`sA^*zTh*!km3|Sc5&@2IZ;ebLgiw&Qhj>|^fF+!^rWVsNs>FHn`g<UoursvXHiI0dDOf%Yp<i>!z)$;4`Fk6h?vbN(?JqUf-<Hck*;nIrYr~))CUmDTEQWAVyT#5p;v&qMWWh`S}x6c#%fr2`N>PrNP`AZceAGf>fR;u*6q(Hhcln<8hWT+cWs>=ksIZwc@rNzeoi@8+9gci@_E!<bXV4NY9dHA(WI&)2nw%!wSm?6Xzh<nR6TEAXf+nku}*(TGw#@l&AKWx_jjeis>lonb@JJX(jt=u~J^B*jl&mVpk5UMEx%=dKj*%PJ%FC=t<2P-$>eKd12#zjjpHB;S(W-4szI_^MRzm~hTE3HdAVaaYZ%gB+QUsjSw&oaFEjl&gD`F@i5uAOniJ>4=n&Eq`~ku?8|ABt#RI%=#!v;7EYp;m9(7HHI-77rJcNQecN0i{uqr)4*h1~))u#w&5!3=Mf6qK;x!7<+hIV24y3vV%g}wlhw8%@<8ESaWK*C|uQ8<vXeGYs<2xeYVORw6g=*a<<plJ(DdRabafJA-kQt%Pd^Z`DAwv43KNN6)w00F$5vkS5{!qq>v_`%1(!F{OSl~HWv=pj$+t1ynZ80T>R?7;Quds4<2rVvj6}"""


def ensure_captcha_helper() -> Path:
    """Найти совместимый helper либо восстановить актуальную встроенную копию."""
    expected = APP_DIR / "vk_captcha_playwright.py"

    def is_compatible(candidate: Path) -> bool:
        try:
            head = candidate.read_text(encoding="utf-8", errors="replace")[:12000]
            return f'HELPER_BUILD = "{_REQUIRED_CAPTCHA_HELPER_BUILD}"' in head
        except (OSError, UnicodeError):
            return False

    if expected.is_file() and is_compatible(expected):
        return expected

    # Не используем просто любой файл с таким именем: он может быть от старой сборки.
    candidates = [Path.cwd() / "vk_captcha_playwright.py"]
    candidates.extend(Path(p) / "vk_captcha_playwright.py" for p in sys.path if p)
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
            if candidate.is_file() and is_compatible(candidate):
                return candidate
        except (OSError, RuntimeError):
            continue

    raw = gzip.decompress(base64.b85decode("".join(_Captcha_HELPER_B85.split()).encode("ascii")))
    try:
        expected.parent.mkdir(parents=True, exist_ok=True)
        temp_path = expected.with_suffix(expected.suffix + ".tmp")
        temp_path.write_bytes(raw)
        temp_path.replace(expected)
        return expected
    except OSError:
        # Последний резерв: временная папка пользователя, если папка приложения read-only.
        fallback_dir = Path(tempfile.gettempdir()) / "VK_ID_Calibrator"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback = fallback_dir / "vk_captcha_playwright.py"
        fallback.write_bytes(raw)
        return fallback

CACHE_FILE = APP_DIR / "vk_dates_cache.json"
APP_SETTINGS_FILE = APP_DIR / "vk_id_calibrator_settings.xml"
LEGACY_APP_SETTINGS_FILE = APP_DIR / "vk_calibrator_settings.json"
SESSION_FILE = APP_DIR / "vk_restore_session.json"
RESULTS_FILE = APP_DIR / "vk_calibration_results.json"
SINGLE_RESULTS_FILE = APP_DIR / "vk_single_id_history.json"
DEFAULT_API_URL = "https://api.vk.com/method/"
DEFAULT_BROWSER_PROFILE = str((APP_DIR / "browser_profile").resolve())
DEFAULT_BROWSER_EXECUTABLE = r"C:/Program Files/Google/Chrome/Application/chrome.exe"
MSK = timezone(timedelta(hours=3))


def default_app_settings() -> dict[str, Any]:
    """Значения формы по умолчанию для общего механизма настроек FaceSearch."""
    return {
        "max_id": "950000000",
        "points": "140",
        "pause": "2.0",
        "retries": "4",
        "api_url": DEFAULT_API_URL,
        "build_ranges": True,
        "browser_profile": DEFAULT_BROWSER_PROFILE,
        "browser_executable": DEFAULT_BROWSER_EXECUTABLE,
        "single_id": "",
        "single_use_cache": True,
        "geometry": "1160x820",
        "maximized": False,
    }


# ---------------------------------------------------------------------------
# Хранение данных
# ---------------------------------------------------------------------------

def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass
class ProbeResult:
    uid: int
    timestamp: int | None
    status: str
    detail: str = ""
    from_cache: bool = False

    @property
    def year(self) -> int | None:
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp, MSK).year

    @property
    def date_text(self) -> str:
        if self.timestamp is None:
            return "—"
        return datetime.fromtimestamp(self.timestamp, MSK).strftime("%d.%m.%Y %H:%M")


class VKRestoreClient:
    """Клиент недокументированного механизма получения даты регистрации."""

    def __init__(self, api_url: str, pause: float, retries: int,
                 log: Callable[[str], None], stop_event: threading.Event, browser_profile: str, browser_executable: str):
        self.api_url = api_url.rstrip("/") + "/"
        self.pause = max(0.2, pause)
        self.retries = max(1, retries)
        self.log = log
        self.stop_event = stop_event
        self.browser_profile = browser_profile
        self.browser_executable = browser_executable
        # Актуальный браузерный контекст действует только в памяти процесса.
        # Сами cookies не сохраняются в SESSION_FILE или настройках приложения.
        self.browser_cookie_header = ''
        self.browser_user_agent = ''
        self.browser_client_headers: dict[str, str] = {}
        self._post_captcha_pause = 0.0
        self.session = read_json(SESSION_FILE, {})
        self.cache = read_json(CACHE_FILE, {})
        if not isinstance(self.cache, dict):
            self.cache = {}
        self._cache_lock = threading.Lock()
        self._session_lock = threading.Lock()

    def _wait(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise InterruptedError("Операция остановлена пользователем")

    def _wait_after_request(self) -> None:
        # После ручной CAPTCHA даём сессии дополнительное время стабилизироваться.
        delay = max(self.pause, self._post_captcha_pause)
        self._post_captcha_pause = 0.0
        self._wait(delay)

    def _http_call(self, method: str, params: dict[str, Any], user_agent: str | None = None,
                   cookies: str | None = None) -> dict[str, Any]:
        body = urllib.parse.urlencode(params).encode("utf-8")
        effective_user_agent = user_agent or self.browser_user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        effective_cookies = cookies if cookies is not None else self.browser_cookie_header
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": effective_user_agent,
        }
        # После первого браузерного CAPTCHA-цикла последующие restore.init
        # используют тот же ограниченный cookie/header-контекст VK ID.
        # Значения cookies остаются только в памяти текущего процесса.
        if user_agent or effective_cookies or self.browser_user_agent:
            headers.update({
                "Origin": self.browser_client_headers.get("origin") or "https://id.vk.com",
                "Referer": self.browser_client_headers.get("referer") or "https://id.vk.com/",
                "Accept": "*/*",
                "Accept-Language": self.browser_client_headers.get("accept-language") or "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Fetch-Site": self.browser_client_headers.get("sec-fetch-site") or "same-site",
                "Sec-Fetch-Mode": self.browser_client_headers.get("sec-fetch-mode") or "cors",
                "Sec-Fetch-Dest": self.browser_client_headers.get("sec-fetch-dest") or "empty",
                "Priority": self.browser_client_headers.get("priority") or "u=1, i",
            })
            for header_name in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
                if self.browser_client_headers.get(header_name):
                    headers[header_name.title()] = self.browser_client_headers[header_name]
        if effective_cookies:
            headers["Cookie"] = effective_cookies
        request = urllib.request.Request(
            self.api_url + method,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8", errors="replace")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise RuntimeError("VK вернул ответ неожиданного формата")
            return result
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {body_text or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ошибка соединения: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("VK вернул некорректный JSON") from exc

    def get_session(self, force: bool = False) -> dict[str, Any]:
        with self._session_lock:
            if not force and self.session.get("restore_session_id"):
                return self.session
            response = self._http_call("restore.getSettings", {
                "type": "restore",
                "vkui": 1,
                "platform": "vkcom",
                "access_token": "",
                "v": "5.83",
            })
            data = response.get("response") or {}
            if not data.get("restore_session_id"):
                raise RuntimeError(f"restore.getSettings не вернул restore_session_id: {response}")
            self.session = {
                "restore_session_id": data["restore_session_id"],
                "unauth_id": data.get("unauth_id", ""),
                "saved_at": int(time.time()),
            }
            write_json(SESSION_FILE, self.session)
            return self.session

    @staticmethod
    def _parse_date(response: dict[str, Any]) -> int | None:
        value = response.get("response")
        data: Any = None
        # Формат, который использовался исходным скриптом: response[1].user.date_created.
        if isinstance(value, list) and len(value) > 1:
            data = value[1]
        elif isinstance(value, dict):
            data = value
        if not isinstance(data, dict):
            return None
        user = data.get("user")
        if isinstance(user, dict):
            raw = user.get("date_created")
        else:
            raw = data.get("date_created")
        try:
            stamp = int(raw)
            if stamp <= 0:
                return None
            return stamp
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _validate_captcha_url(redirect_uri: str) -> str:
        """Разрешаем открывать только штатную страницу проверки VK ID."""
        parsed = urlparse(redirect_uri)
        if (parsed.scheme != "https" or parsed.hostname not in {"id.vk.ru", "id.vk.com"}
                or parsed.path.rstrip("/") != "/not_robot_captcha"):
            raise RuntimeError("VK вернул неожиданный адрес CAPTCHA. Открытие заблокировано.")
        return redirect_uri

    @staticmethod
    def _captcha_ts_param(value: Any) -> str:
        """Нормализует captcha_ts так же, как VK web-клиенты (без лишнего .0)."""
        try:
            number = float(value) if value not in (None, "") else 0.0
            if number.is_integer():
                return str(int(number))
            return format(number, ".15g")
        except (TypeError, ValueError, OverflowError):
            return "0"

    @staticmethod
    def _captcha_attempt_param(value: Any) -> str:
        """Нормализует captcha_attempt: числа 1.0 передаются как '1', не '1.0'."""
        if value in (None, ""):
            return "1"
        if isinstance(value, str):
            return value
        try:
            return str(int(value))
        except (TypeError, ValueError, OverflowError):
            return "1"

    def _solve_checkbox_captcha(self, redirect_uri: str, retry_params: dict[str, Any]) -> dict[str, Any]:
        """Проходит SmartCaptcha и повторяет restore.init прямо внутри Chrome."""
        redirect_uri = self._validate_captcha_url(redirect_uri)
        try:
            helper = ensure_captcha_helper()
            self.log(f"Модуль CAPTCHA: {helper}")
        except Exception as exc:
            raise RuntimeError(
                "Не удалось восстановить вспомогательный модуль CAPTCHA. "
                f"Основной файл: {Path(__file__).resolve()}; каталог приложения: {APP_DIR}; "
                f"причина: {type(exc).__name__}: {exc}"
            ) from exc

        fd, result_path_text = tempfile.mkstemp(prefix="vk_captcha_", suffix=".json")
        os.close(fd)
        result_path = Path(result_path_text)
        process_log_path = result_path.with_suffix(".process.log")
        process_log_file = None
        try:
            self.log("VK запросил интерактивную проверку. Запускаю отдельное окно Chromium; поставьте галочку вручную.")
            self.log("Перед CAPTCHA повторно создаю restore.init внутри Chrome, чтобы исходный запрос и проверка использовали одну cookie-сессию.")
            self.log("Адрес проверки и служебные токены не записываются в журнал и не сохраняются в результатах.")
            self.log(f"Запускаю браузер через Python: {sys.executable}")
            self.log(f"Основной файл приложения: {Path(__file__).resolve()}")
            self.log(f"Рабочий каталог приложения: {APP_DIR}")
            try:
                process_log_file = process_log_path.open("w", encoding="utf-8", errors="replace")
                process = subprocess.Popen(
                    [sys.executable, str(helper), str(result_path)],
                    stdin=subprocess.PIPE,
                    stdout=process_log_file,
                    stderr=subprocess.STDOUT,
                    cwd=str(APP_DIR),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise RuntimeError(
                    f"Не удалось запустить модуль Playwright: {type(exc).__name__}: {exc}. "
                    f"Python: {sys.executable}"
                ) from exc

            try:
                assert process.stdin is not None
                retry_url = self.api_url + "restore.init"
                process.stdin.write(json.dumps({
                    "url": redirect_uri,
                    "profile_dir": self.browser_profile,
                    "browser_executable": self.browser_executable,
                    "retry_url": retry_url,
                    "retry_params": retry_params,
                }, ensure_ascii=False).encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                try:
                    process.terminate()
                except OSError:
                    pass
                if process_log_file is not None:
                    try:
                        process_log_file.flush()
                        process_log_file.close()
                        process_log_file = None
                    except OSError:
                        pass
                child_output = ""
                try:
                    child_output = process_log_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    pass
                if child_output:
                    for line in child_output.splitlines()[-15:]:
                        self.log(f"  вывод Playwright: {line[:500]}")
                raise RuntimeError(
                    f"Процесс Playwright завершился до передачи параметров ({type(exc).__name__}: {exc}). "
                    "См. диагностику выше. Проверьте Python, Playwright и файл vk_captcha_playwright.py."
                ) from exc

            captcha_started = time.monotonic()
            token_notice_logged = False
            while process.poll() is None:
                if time.monotonic() - captcha_started > 900:
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            process.kill()
                        except OSError:
                            pass
                    raise RuntimeError("Время ожидания CAPTCHA истекло (15 минут). Повторите запрос.")
                if self.stop_event.wait(0.25):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise InterruptedError("Операция остановлена пользователем во время CAPTCHA")
                result = read_json(result_path, {})
                if isinstance(result, dict) and result.get("token_obtained") and not token_notice_logged:
                    token_notice_logged = True
                    self.log("VK вернул success_token. Повтор restore.init будет выполнен из того же окна Chrome.")

            # Helper дожидается captchaNotRobot.endSession, закрывает Chromium
            # и только после этого завершает работу.
            if process_log_file is not None:
                process_log_file.flush()
                process_log_file.close()
                process_log_file = None
            result = read_json(result_path, {})
            if isinstance(result, dict) and result.get("retry_response") is not None:
                response_data = result.get("retry_response")
                if not isinstance(response_data, dict):
                    raise RuntimeError("Браузер выполнил CAPTCHA, но ответ restore.init имеет неверный формат.")
                browser_context = result.get("browser_context")
                if isinstance(browser_context, dict):
                    cookie_header = str(browser_context.get("cookie_header") or "").strip()
                    user_agent_value = str(browser_context.get("user_agent") or "").strip()
                    client_headers = browser_context.get("headers")
                    if cookie_header:
                        self.browser_cookie_header = cookie_header
                    if user_agent_value:
                        self.browser_user_agent = user_agent_value
                    if isinstance(client_headers, dict):
                        self.browser_client_headers = {
                            str(k): str(v) for k, v in client_headers.items()
                            if k in {"origin", "referer", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "priority"} and v
                        }
                    if cookie_header or user_agent_value:
                        names = browser_context.get("cookie_names")
                        names_text = ", ".join(str(x) for x in names) if isinstance(names, list) else "обновлены"
                        self.log("Браузерный контекст сохранён в памяти для следующих запросов: " + (names_text or "cookie не обнаружены") + ".")
                self._post_captcha_pause = max(self.pause, 4.0) if result.get("captcha_performed") is not False else self.pause
                session_update = result.get("session_update")
                if isinstance(session_update, dict) and session_update.get("restore_session_id"):
                    self.session = {
                        "restore_session_id": session_update["restore_session_id"],
                        "unauth_id": session_update.get("unauth_id", ""),
                        "saved_at": int(time.time()),
                    }
                    write_json(SESSION_FILE, self.session)
                    self.log("Параметры restore-сессии обновлены из браузерного контекста.")
                if result.get("captcha_performed") is False:
                    self.log("Первоначальный restore.init из Chrome выполнен без CAPTCHA.")
                else:
                    self.log("CAPTCHA пройдена; повтор restore.init выполнен в той же cookie-сессии Chrome.")
                if result.get("captcha_performed") is not False and result.get("end_session_ok"):
                    self.log(f"Завершение CAPTCHA-сессии: captchaNotRobot.endSession — OK (HTTP {result.get('end_session_http_status', '?')}).")
                elif result.get("captcha_performed") is not False:
                    self.log("ПРЕДУПРЕЖДЕНИЕ: captchaNotRobot.endSession не подтверждён; запрос restore.init всё равно отправлен из Chrome.")
                self.log(f"restore.init через Chrome: HTTP {result.get('retry_http_status', '?')}.")
                diag = result.get("retry_diagnostics")
                if isinstance(diag, dict):
                    self.log(
                        "Первичный restore.init в Chrome: "
                        f"запрос замечен={'да' if diag.get('browser_initial_init_seen') else 'нет'}, "
                        f"remixstlid={'есть' if diag.get('browser_initial_remixstlid_present') else 'нет'}, "
                        f"Origin={diag.get('browser_initial_origin') or 'не определён'}; "
                        f"этот remixstlid совпал с CAPTCHA check={'да' if diag.get('browser_initial_vs_check_remixstlid_matches') else 'нет/не подтверждено'}, "
                        f"User-Agent совпал={'да' if diag.get('browser_initial_vs_check_user_agent_matches') else 'нет/не подтверждено'}."
                    )
                    self.log(
                        "Привязка CAPTCHA: "
                        f"check session_token совпадает с redirect_uri={'да' if diag.get('check_session_matches_challenge') else 'нет/не подтверждено'}, "
                        f"check status={diag.get('check_vk_status') or 'неизвестен'}, "
                        f"endSession относится к тому же challenge={'да' if diag.get('end_session_session_matches_challenge') else 'нет/не подтверждено'}."
                    )
                    self.log(
                        "Cookies CAPTCHA/retry: "
                        f"remixstlid check/retry={'есть' if diag.get('check_remixstlid_present') and diag.get('retry_remixstlid_present') else 'отсутствует'}, "
                        f"совпадает={'да' if diag.get('retry_session_cookie_matches_check') else 'нет/не подтверждено'}; "
                        f"remixstid check/retry={'есть' if diag.get('check_remixstid_present') and diag.get('retry_remixstid_present') else 'отсутствует'}, "
                        f"совпадает={'да' if diag.get('retry_remixstid_matches_check') else 'нет/не подтверждено'}; "
                        f"domain_sid check/retry={'есть' if diag.get('check_domain_sid_present') and diag.get('retry_domain_sid_present') else 'отсутствует'}, "
                        f"совпадает={'да' if diag.get('retry_domain_sid_matches_check') else 'нет/не подтверждено'}."
                    )
                    self.log(
                        "Отпечаток браузера: User-Agent check/retry совпадает="
                        f"{'да' if diag.get('retry_user_agent_matches_check') else 'нет/не подтверждено'}; "
                        f"Origin повтора={diag.get('retry_origin') or 'не определён'}; "
                        f"Referer повтора={diag.get('retry_referer') or 'не определён'}."
                    )
                    fields = diag.get('retry_field_matches')
                    if isinstance(fields, dict) and fields:
                        self.log("Совпадение полей формы с подготовленными значениями: " + ", ".join(
                            f"{key}={'да' if value else 'НЕТ'}" for key, value in fields.items()
                        ))
                    echo = diag.get('server_echo_matches')
                    if isinstance(echo, dict) and echo:
                        self.log("Сверка значений request_params, отражённых VK: " + ", ".join(
                            f"{key}={'совпадает' if value else 'РАСХОЖДЕНИЕ'}" for key, value in echo.items()
                        ))
                    elif isinstance(fields, dict) and fields:
                        self.log("VK не вернул значения request_params для дополнительной сверки.")
                return response_data
            if isinstance(result, dict) and result.get("retry_error"):
                raise RuntimeError("Не удалось выполнить повтор restore.init из Chrome: " + str(result.get("retry_error")))
            detail = result.get("error") if isinstance(result, dict) else None
            if detail:
                self.log(f"Ошибка запуска/работы Chromium: {detail}")
                diagnostics = result.get("diagnostics") if isinstance(result, dict) else None
                if diagnostics:
                    for line in str(diagnostics).splitlines():
                        self.log(f"  диагностика: {line}")
                safe_diag = result.get("retry_diagnostics") if isinstance(result, dict) else None
                if isinstance(safe_diag, dict):
                    self.log(
                        "Диагностика challenge: "
                        f"check session_token совпадает={'да' if safe_diag.get('check_session_matches_challenge') else 'нет/не подтверждено'}, "
                        f"remixstlid на check={'есть' if safe_diag.get('check_remixstlid_present') else 'нет'}; "
                        f"endSession session_token совпадает={'да' if safe_diag.get('end_session_session_matches_challenge') else 'нет/не подтверждено'}."
                    )
                raise RuntimeError(f"Не удалось открыть CAPTCHA в Chromium: {detail}")
            return_code = process.poll()
            process_output = ""
            try:
                process_output = process_log_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            if process_output:
                for line in process_output.splitlines()[-15:]:
                    self.log(f"  вывод Playwright: {line[:500]}")
            if return_code not in (None, 0):
                raise RuntimeError(
                    f"Процесс браузера завершился с кодом {return_code} без результата. "
                    "См. строки вывода Playwright выше."
                )
            raise RuntimeError("Окно CAPTCHA закрыто до завершения проверки. Калибровка остановлена на текущем ID.")
        finally:
            if process_log_file is not None:
                try:
                    process_log_file.close()
                except OSError:
                    pass
            for temporary_path in (result_path, process_log_path):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def get_date(self, uid: int, use_cache: bool = True) -> ProbeResult:
        key = str(uid)
        if use_cache:
            with self._cache_lock:
                cached = self.cache.get(key)
            if cached:
                try:
                    return ProbeResult(uid, int(cached), "ok", from_cache=True)
                except (TypeError, ValueError):
                    pass

        last_error = ""
        for attempt in range(self.retries):
            if self.stop_event.is_set():
                raise InterruptedError("Операция остановлена пользователем")
            try:
                session = self.get_session()
                request_params = {
                    "uid": uid,
                    "access_token": "",
                    "restore_session_id": session["restore_session_id"],
                    "unauth_id": session.get("unauth_id", ""),
                    "v": "5.223",
                }
                response = self._http_call("restore.init", request_params)
                error = response.get("error")
                if error:
                    code = error.get("error_code")
                    msg = str(error.get("error_msg", "ошибка API"))
                    if code == 14:
                        redirect_uri = error.get("redirect_uri")
                        if redirect_uri:
                            # Новая интерактивная CAPTCHA VK ID: пользователь сам
                            # проходит checkbox в отдельном Chromium; токен применяется
                            # только к повтору того же исходного запроса.
                            # Для SmartCaptcha нужен не только success_token, но и тройка
                            # challenge, к которому он привязан: captcha_sid/captcha_ts/
                            # captcha_attempt. Повтор отправляется из самой страницы Chrome,
                            # чтобы TLS, Client-Hints, cookies и заголовки совпадали с check.
                            retry_params = dict(request_params)
                            retry_params.update({
                                "captcha_key": "",
                                "captcha_sid": str(error.get("captcha_sid") or ""),
                                "is_sound_captcha": "0",
                                "captcha_ts": self._captcha_ts_param(error.get("captcha_ts")),
                                "captcha_attempt": self._captcha_attempt_param(error.get("captcha_attempt")),
                            })
                            self.log(
                                "Параметры challenge: "
                                f"captcha_sid={'получен' if retry_params['captcha_sid'] else 'отсутствует'}, "
                                f"captcha_ts={'получен' if error.get('captcha_ts') not in (None, '') else 'значение 0'}, "
                                f"captcha_attempt={'получен' if error.get('captcha_attempt') not in (None, '') else 'значение 1'}."
                            )
                            response = self._solve_checkbox_captcha(str(redirect_uri), retry_params)
                            retry_error = response.get("error")
                            if retry_error:
                                retry_code = retry_error.get("error_code")
                                retry_msg = str(retry_error.get("error_msg", "ошибка API"))
                                echoed = retry_error.get("request_params")
                                echoed_keys = []
                                if isinstance(echoed, list):
                                    echoed_keys = [str(item.get("key")) for item in echoed
                                                   if isinstance(item, dict) and item.get("key")]
                                elif isinstance(echoed, dict):
                                    echoed_keys = [str(k) for k in echoed.keys()]
                                if echoed_keys:
                                    # Логируем только имена параметров, никогда не значения.
                                    self.log("VK отразил параметры повтора (только имена): " + ", ".join(echoed_keys))
                                self.log("VK вернул отказ на повтор restore.init, отправленный из Chrome.")
                                raise RuntimeError(
                                    f"VK не принял подтверждение CAPTCHA (код {retry_code}): {retry_msg}. "
                                    "CAPTCHA подтверждена браузером, но VK не принял повтор. Проверьте параметры challenge."
                                )
                        else:
                            # Старый тип капчи (captcha_sid/captcha_img) отличается
                            # от checkbox-проверки и не должен обрабатываться как новая.
                            captcha_img = str(error.get("captcha_img", ""))
                            captcha_hint = " VK приложил изображение CAPTCHA." if captcha_img else ""
                            raise RuntimeError(
                                "VK вернул классическую CAPTCHA без redirect_uri; "
                                "этот ответ не является checkbox-проверкой." + captcha_hint
                            )
                    elif code in (15, 100) or "not found" in msg.lower() or "не найден" in msg.lower():
                        self._wait_after_request()
                        return ProbeResult(uid, None, "unavailable", f"API {code}: {msg}")
                    else:
                        last_error = f"API {code}: {msg}"
                        if attempt == 0:
                            self.get_session(force=True)
                        self._wait(min(3, attempt + 1))
                        continue

                stamp = self._parse_date(response)
                if stamp is None:
                    last_error = f"Неожиданный ответ: {str(response)[:250]}"
                    self._wait_after_request()
                    continue

                with self._cache_lock:
                    self.cache[key] = stamp
                    write_json(CACHE_FILE, self.cache)
                self._wait_after_request()
                return ProbeResult(uid, stamp, "ok")
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = str(exc)
                # Ошибки интерактивной проверки не повторяем автоматически:
                # пользователь должен иметь возможность исправить причину.
                if "CAPTCHA" in last_error or "captcha" in last_error.lower():
                    return ProbeResult(uid, None, "error", last_error)
                if attempt + 1 < self.retries:
                    self.log(f"ID {uid:,}: {last_error}. Повтор {attempt + 1}/{self.retries}.")
                    self._wait(min(4, attempt + 1))

        return ProbeResult(uid, None, "error", last_error or "Не удалось получить дату")


# ---------------------------------------------------------------------------
# Калибровка
# ---------------------------------------------------------------------------

class CalibrationWorker:
    def __init__(self, config: dict[str, Any], events: queue.Queue,
                 stop_event: threading.Event, client: VKRestoreClient | None = None):
        self.config = config
        self.events = events
        self.stop_event = stop_event
        self.client = client or VKRestoreClient(
            config["api_url"], config["pause"], config["retries"],
            lambda message: self.emit("log", message), stop_event, config["browser_profile"], config["browser_executable"],
        )
        self.anchors: list[ProbeResult] = []
        self.ranges: list[dict[str, Any]] = []
        self.previous_results = read_json(RESULTS_FILE, {})
        if not isinstance(self.previous_results, dict):
            self.previous_results = {}
        saved_plan = self.previous_results.get("calibration", {})
        self.saved_calibration = saved_plan if isinstance(saved_plan, dict) else {}
        self.calibration_state: dict[str, Any] = {}
        self.saved_anchors: dict[int, ProbeResult] = {}
        anchor_priority = {"error": 0, "unavailable": 1, "ok": 2}
        for item in self.previous_results.get("anchors", []):
            if not isinstance(item, dict):
                continue
            try:
                uid = int(item["id"])
                status = str(item.get("status", "ok"))
                raw_timestamp = item.get("timestamp")
                timestamp = int(raw_timestamp) if raw_timestamp is not None else None
                if uid < 1 or status not in {"ok", "unavailable", "error"}:
                    continue
                if status == "ok" and timestamp is None:
                    continue
                candidate = ProbeResult(uid, timestamp, status, str(item.get("detail", "")))
                current = self.saved_anchors.get(uid)
                if current is None or anchor_priority.get(status, 0) > anchor_priority.get(current.status, 0):
                    self.saved_anchors[uid] = candidate
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    def emit(self, kind: str, data: Any = None) -> None:
        self.events.put((kind, data))

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise InterruptedError("Операция остановлена пользователем")

    def find_top(self, max_id: int) -> ProbeResult:
        uid = max_id
        attempts = 0
        while uid >= 1:
            self.check_stop()
            attempts += 1
            self.emit("status", f"Поиск верхней опорной точки: проверяется ID {uid:,}")
            self.emit("log", f"Проверка верхней границы: ID {uid:,}")
            result = self.client.get_date(uid)
            if result.status == "ok":
                return result
            if result.status == "error":
                raise RuntimeError(f"Не удалось проверить верхнюю границу ID {uid:,}: {result.detail}")
            uid = int(uid * 0.85)
            if attempts > 100:
                break
        raise RuntimeError("Не удалось найти доступный ID. Проверьте работу restore.init и адрес API.")

    def build_anchors(self, top: ProbeResult, target_ids: list[int]) -> None:
        """Продолжает сохранённую сетку, запрашивая только ещё не полученные ID."""
        ordered_ids = sorted({int(uid) for uid in target_ids if int(uid) > 0} | {top.uid})
        completed: dict[int, ProbeResult] = {}
        for uid in ordered_ids:
            if uid == top.uid:
                completed[uid] = top
                continue
            saved = self.saved_anchors.get(uid)
            # Успешные ответы и подтверждённое отсутствие профиля уже обработаны.
            # Ошибки остаются в очереди для повторной попытки.
            if saved is not None and saved.status in {"ok", "unavailable"}:
                completed[uid] = saved

        self.anchors = sorted(completed.values(), key=lambda item: item.uid)
        total = len(ordered_ids)
        self.emit("progress_init", total)
        ready = len(completed)
        remaining = total - ready
        if remaining:
            self.emit("log", f"Продолжение калибровки: уже получено {ready} из {total} опорных точек; осталось {remaining}.")
        else:
            self.emit("log", f"Все {total} опорных точек уже сохранены. Повторные запросы пропущены.")

        # Фиксируем восстановленные точки и верхнюю границу до новых запросов.
        for item in self.anchors:
            self.emit("anchor", item)
        self.save_results()
        self.emit("progress", (ready, total, f"Восстановлено точек: {ready}/{total}"))

        processed = set(completed)
        for index, uid in enumerate(ordered_ids, 1):
            self.check_stop()
            if uid in processed:
                self.emit("progress", (index, total, f"Опорные точки: {index}/{total} (уже сохранено)"))
                continue

            result = self.client.get_date(uid)
            self.anchors.append(result)
            self.anchors.sort(key=lambda item: item.uid)
            processed.add(uid)
            self.emit("anchor", result)

            # Сохраняем каждую точку сразу: аварийное закрытие не сбросит прогресс.
            self.save_results()
            if result.status == "error":
                self.emit("log", f"ID {uid:,}: ошибка запроса — {result.detail}")
            elif result.status != "ok":
                self.emit("log", f"ID {uid:,}: дата недоступна ({result.detail})")
            else:
                self.emit("log", f"ID {uid:,} → {result.date_text} (год {result.year})" + (" [кэш]" if result.from_cache else ""))
            self.emit("progress", (index, total, f"Опорные точки: {index}/{total}"))

    @staticmethod
    def anchors_monotonic(anchors: list[ProbeResult]) -> tuple[bool, str]:
        valid = [item for item in anchors if item.timestamp is not None]
        problems = []
        for left, right in zip(valid, valid[1:]):
            if right.timestamp < left.timestamp:
                problems.append(f"{left.uid:,} ({left.date_text}) → {right.uid:,} ({right.date_text})")
                if len(problems) == 3:
                    break
        return not problems, "; ".join(problems)

    def probe_near(self, middle: int, low: int, high: int) -> ProbeResult | None:
        """При дырке проверяем соседние ID, не делая вывод по пустому ответу."""
        offsets = (1, -1, 2, -2, 4, -4, 8, -8, 16, -16)
        for offset in offsets:
            candidate = middle + offset
            if low < candidate < high:
                self.check_stop()
                result = self.client.get_date(candidate)
                if result.status == "ok":
                    self.emit("log", f"ID {middle:,} недоступен; использована соседняя точка {candidate:,}")
                    return result
                if result.status == "error":
                    raise RuntimeError(f"Ошибка при проверке ID {candidate:,}: {result.detail}")
        return None

    def find_year_boundary(self, year: int, anchors: list[ProbeResult]) -> tuple[int | None, str]:
        target = int(datetime(year, 1, 1, tzinfo=MSK).timestamp())
        valid = sorted((a for a in anchors if a.timestamp is not None), key=lambda a: a.uid)
        before = [a for a in valid if a.timestamp < target]
        after = [a for a in valid if a.timestamp >= target]
        if not after:
            return None, "нет верхней опорной точки"
        low = before[-1].uid if before else 0
        high = after[0].uid
        if high <= low:
            return None, "не удалось сформировать интервал"

        used_neighbour = False
        max_steps = max(1, (high - low).bit_length() + 4)
        for _ in range(max_steps):
            self.check_stop()
            if high - low <= 1:
                # Если в календарном году не было регистраций, бинарный поиск
                # упрётся в первую точку следующего года. Проверяем саму дату.
                end_probe = self.client.get_date(high)
                if end_probe.status != "ok":
                    return None, f"не удалось подтвердить дату граничного ID {high:,}"
                if end_probe.year != year:
                    return None, f"нет подтверждённой регистрации в {year} году; ближайшая дата — {end_probe.date_text}"
                return high, "уточнено бинарным поиском" + (" с обходом пропусков" if used_neighbour else "")
            middle = (low + high) // 2
            result = self.client.get_date(middle)
            if result.status != "ok":
                result = self.probe_near(middle, low, high)
                if result is None:
                    return None, f"нет данных около ID {middle:,}; граница не подтверждена"
                used_neighbour = True
            if result.timestamp >= target:
                high = result.uid
            else:
                low = result.uid
        return None, "достигнут предел уточнения"

    def build_ranges(self) -> None:
        valid = [a for a in self.anchors if a.timestamp is not None]
        if len(valid) < 2:
            self.emit("log", "Недостаточно успешных опорных точек для расчёта диапазонов.")
            return
        monotonic, details = self.anchors_monotonic(self.anchors)
        if not monotonic:
            self.emit("warning", "Даты опорных точек не монотонны по ID. Диапазоны не рассчитывались. Примеры: " + details)
            return

        years = range(min(a.year for a in valid), max(a.year for a in valid) + 1)
        years = list(years)
        self.emit("progress_init", len(years))
        boundaries: dict[int, int] = {}
        statuses: dict[int, str] = {}
        for index, year in enumerate(years, 1):
            self.check_stop()
            self.emit("status", f"Поиск первой регистрации за {year} год")
            boundary, info = self.find_year_boundary(year, self.anchors)
            statuses[year] = info
            if boundary is not None:
                boundaries[year] = boundary
                self.emit("log", f"{year}: первый ID ≈ {boundary:,} ({info})")
            else:
                self.emit("log", f"{year}: диапазон не определён — {info}")
            self.emit("progress", (index, len(years), f"Границы годов: {index}/{len(years)}"))

        sorted_bounds = sorted(boundaries.items())
        for i, (year, start_id) in enumerate(sorted_bounds):
            next_start = sorted_bounds[i + 1][1] if i + 1 < len(sorted_bounds) else max(a.uid for a in valid) + 1
            row = {
                "year": year,
                "id_from": start_id,
                "id_to": max(start_id, next_start - 1),
                "status": statuses.get(year, ""),
            }
            self.ranges.append(row)
            self.emit("range", row)

    def run(self) -> None:
        try:
            max_id = int(self.config["max_id"])
            count = int(self.config["points"])
            api_url = str(self.config.get("api_url") or DEFAULT_API_URL)
            self.emit("status", "Получение сессии VK…")
            self.client.get_session()
            self.emit("log", "Сессия VK получена.")

            # Для возобновления используем прежнюю сетку. Иначе новый верхний
            # ID сместил бы интервалы и заставил повторять уже пройденные точки.
            plan = self.saved_calibration
            try:
                same_plan = (
                    int(plan.get("max_id")) == max_id
                    and int(plan.get("points")) == count
                    and str(plan.get("api_url") or DEFAULT_API_URL) == api_url
                    and isinstance(plan.get("target_ids"), list)
                    and bool(plan.get("target_ids"))
                )
            except (TypeError, ValueError):
                same_plan = False

            top: ProbeResult | None = None
            target_ids: list[int] = []
            if same_plan:
                try:
                    saved_ids = sorted({int(value) for value in plan["target_ids"] if int(value) > 0})
                    saved_top_id = int(plan.get("top_id") or 0)
                    candidate = self.saved_anchors.get(saved_top_id)
                    if candidate is not None and candidate.status == "ok" and candidate.timestamp is not None:
                        top = candidate
                    elif saved_top_id > 0:
                        candidate = self.client.get_date(saved_top_id)
                        if candidate.status == "ok":
                            top = candidate
                    if top is not None and saved_ids:
                        target_ids = sorted(set(saved_ids) | {top.uid})
                        self.emit("log", f"Возобновляю прежнюю сетку опорных точек (верхний ID {top.uid:,}; запрошено {len(target_ids)} точек).")
                except (TypeError, ValueError):
                    top = None
                    target_ids = []

            if top is None:
                self.emit("status", "Поиск верхнего существующего ID…")
                top = self.find_top(max_id)
                target_ids = {
                    max(1, int(round(1 + (top.uid - 1) * i / max(1, count - 1))))
                    for i in range(count)
                }
                target_ids.add(top.uid)
                target_ids = sorted(target_ids)
                self.emit("log", f"Верхняя опорная точка: ID {top.uid:,} → {top.date_text}")
                self.emit("log", f"Создана новая сетка из {len(target_ids)} опорных точек.")

            self.calibration_state = {
                "max_id": max_id,
                "points": count,
                "api_url": api_url,
                "top_id": top.uid,
                "target_ids": target_ids,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self.emit("top", top)
            self.save_results()
            self.build_anchors(top, target_ids)
            if self.config["build_ranges"]:
                self.build_ranges()
            self.save_results()
            self.emit("done", {"anchors": len(self.anchors), "ranges": len(self.ranges), "stopped": False})
        except InterruptedError:
            self.save_results()
            self.emit("done", {"anchors": len(self.anchors), "ranges": len(self.ranges), "stopped": True})
        except Exception as exc:
            self.save_results()
            self.emit("fatal", str(exc))

    def save_results(self) -> None:
        data = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "timezone": "Europe/Moscow (UTC+03:00)",
            "anchors": [
                {"id": a.uid, "timestamp": a.timestamp, "date": a.date_text,
                 "year": a.year, "status": a.status, "detail": a.detail}
                for a in self.anchors
            ],
            "ranges": self.ranges,
        }
        previous = read_json(RESULTS_FILE, {})
        if not isinstance(previous, dict):
            previous = {}
        if self.calibration_state:
            data["calibration"] = dict(self.calibration_state)
        elif isinstance(previous.get("calibration"), dict):
            data["calibration"] = previous["calibration"]

        def merge_rows(old_rows: Any, new_rows: list[dict[str, Any]],
                       key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, ...]] = set()
            old = old_rows if isinstance(old_rows, list) else []
            for item in [*old, *new_rows]:
                if not isinstance(item, dict):
                    continue
                key = tuple(
                    json.dumps(item.get(field), ensure_ascii=False, sort_keys=True)
                    for field in key_fields
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            return merged

        # Не заменяем предыдущую калибровку: сохраняем все разные варианты
        # опорных точек и диапазонов, добавляя только действительно новые записи.
        data["anchors"] = merge_rows(
            previous.get("anchors", []), data["anchors"],
            ("id", "timestamp", "status", "detail"),
        )
        data["ranges"] = merge_rows(
            previous.get("ranges", []), data["ranges"],
            ("year", "id_from", "id_to", "status"),
        )
        for key, value in previous.items():
            if key not in data:
                data[key] = value
        write_json(RESULTS_FILE, data)


class SingleProbeWorker:
    """Асинхронно получает дату регистрации одного заданного VK ID."""

    def __init__(self, uid: int, use_cache: bool, client: VKRestoreClient,
                 events: queue.Queue, stop_event: threading.Event):
        self.uid = uid
        self.use_cache = use_cache
        self.client = client
        self.events = events
        self.stop_event = stop_event

    def emit(self, kind: str, data: Any = None) -> None:
        self.events.put((kind, data))

    def run(self) -> None:
        try:
            self.emit("status", f"Получение даты регистрации для ID {self.uid:,}…")
            result = self.client.get_date(self.uid, use_cache=self.use_cache)
            self.emit("single_result", result)
            self.emit("single_done", {"stopped": False})
        except InterruptedError:
            self.emit("single_done", {"stopped": True})
        except Exception as exc:
            self.emit("single_failure", str(exc))
            self.emit("single_done", {"stopped": False})


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------

class VKCalibratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VK ID Calibrator — опорные ID и годы регистрации")
        self.minsize(930, 650)
        self._settings_after_id = None
        self._settings_defaults = default_app_settings()
        self._saved_settings = load_module_settings(
            APP_SETTINGS_FILE,
            "vk_id_calibrator",
            self._settings_defaults,
            window_keys={"geometry", "maximized"},
            legacy_json_paths=(LEGACY_APP_SETTINGS_FILE,),
        )
        saved_geometry = str(self._saved_settings.get("geometry") or "1160x820")
        if re.fullmatch(r"\d+x\d+(?:[+-]\d+[+-]\d+)?", saved_geometry):
            self.geometry(saved_geometry)
            self._last_normal_geometry = saved_geometry
        else:
            self.geometry("1160x820")
            self._last_normal_geometry = self.geometry()
        self._restore_maximized = bool(self._saved_settings.get("maximized", False))

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.worker: CalibrationWorker | SingleProbeWorker | None = None
        self.current_task: str | None = None
        self.shared_client: VKRestoreClient | None = None
        self.shared_client_signature: tuple[str, str, str] | None = None
        self.anchor_count = 0
        self.range_count = 0
        self._anchor_rows_seen: set[tuple[Any, ...]] = set()
        self._range_rows_seen: set[tuple[Any, ...]] = set()
        self.single_history: list[dict[str, Any]] = []

        self.max_id_var = tk.StringVar(value=str(self._saved_settings["max_id"]))
        self.points_var = tk.StringVar(value=str(self._saved_settings["points"]))
        self.pause_var = tk.StringVar(value=str(self._saved_settings["pause"]))
        self.retries_var = tk.StringVar(value=str(self._saved_settings["retries"]))
        self.api_var = tk.StringVar(value=str(self._saved_settings["api_url"]))
        self.browser_profile_var = tk.StringVar(value=str(self._saved_settings["browser_profile"]))
        self.browser_executable_var = tk.StringVar(value=str(self._saved_settings["browser_executable"]))
        self.ranges_var = tk.BooleanVar(value=bool(self._saved_settings["build_ranges"]))
        self.single_id_var = tk.StringVar(value=str(self._saved_settings["single_id"]))
        self.single_use_cache_var = tk.BooleanVar(value=bool(self._saved_settings["single_use_cache"]))
        self.status_var = tk.StringVar(value="Готово к работе")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()
        self._load_previous_results()
        self._load_single_history()
        for variable in (
            self.max_id_var, self.points_var, self.pause_var, self.retries_var,
            self.api_var, self.browser_profile_var, self.browser_executable_var,
            self.ranges_var, self.single_id_var, self.single_use_cache_var,
        ):
            variable.trace_add("write", self.schedule_settings_save)
        self.bind("<Configure>", self._on_configure, add="+")
        self.after(180, self._restore_window_state)
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("Treeview", rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(root, text="Параметры калибровки", padding=8)
        settings.pack(fill="x")
        for col in range(10):
            settings.columnconfigure(col, weight=0)
        settings.columnconfigure(9, weight=1)

        ttk.Label(settings, text="Максимальный ID для поиска:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=3)
        self.max_id_entry = ttk.Entry(settings, textvariable=self.max_id_var, width=13)
        self.max_id_entry.grid(row=0, column=1, sticky="w", padx=(0, 14))
        self._attach_entry_clipboard(self.max_id_entry)
        ttk.Label(settings, text="Опорных точек:").grid(row=0, column=2, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.points_var, width=7).grid(row=0, column=3, sticky="w", padx=(0, 14))
        ttk.Label(settings, text="Пауза между запросами, сек:").grid(row=0, column=4, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.pause_var, width=6).grid(row=0, column=5, sticky="w", padx=(0, 14))
        ttk.Label(settings, text="Повторов:").grid(row=0, column=6, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.retries_var, width=5).grid(row=0, column=7, sticky="w")

        ttk.Label(settings, text="Адрес API:").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.api_var).grid(row=1, column=1, columnspan=7, sticky="ew", pady=(7, 2))
        ttk.Checkbutton(settings, text="После опорных точек рассчитать границы диапазонов по годам",
                        variable=self.ranges_var).grid(row=2, column=0, columnspan=8, sticky="w", pady=(7, 0))
        ttk.Label(settings, text="Профиль браузера CAPTCHA:").grid(row=3, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.browser_profile_var).grid(row=3, column=1, columnspan=6, sticky="ew", pady=(7, 2))
        ttk.Button(settings, text="Обзор…", command=self._choose_browser_profile).grid(row=3, column=7, sticky="w", padx=(6, 0), pady=(7, 2))
        ttk.Label(settings, text="Исполняемый файл браузера CAPTCHA:").grid(row=4, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.browser_executable_var).grid(row=4, column=1, columnspan=6, sticky="ew", pady=(7, 2))
        ttk.Button(settings, text="Обзор…", command=self._choose_browser_executable).grid(row=4, column=7, sticky="w", padx=(6, 0), pady=(7, 2))
        ttk.Label(settings, text="Профиль сохраняет cookies и вход VK; для браузера можно указать установленный Chrome или оставить поле пустым для Chromium Playwright.",
                  foreground="#666666").grid(row=5, column=1, columnspan=7, sticky="w", pady=(0, 2))

        single_frame = ttk.LabelFrame(root, text="Получить дату регистрации одного ID", padding=7)
        single_frame.pack(fill="x", pady=(7, 0))
        ttk.Label(single_frame, text="VK ID:").pack(side="left")
        self.single_id_entry = ttk.Entry(single_frame, textvariable=self.single_id_var, width=18)
        self.single_id_entry.pack(side="left", padx=(6, 8))
        self._attach_entry_clipboard(self.single_id_entry)
        self.single_lookup_button = ttk.Button(single_frame, text="Получить дату", command=self.start_single_lookup)
        self.single_lookup_button.pack(side="left")
        ttk.Checkbutton(single_frame, text="Использовать кэш (если есть)",
                        variable=self.single_use_cache_var).pack(side="left", padx=(12, 0))
        self.single_id_entry.bind("<Return>", lambda _event: self.start_single_lookup())

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(9, 7))
        self.start_button = ttk.Button(buttons, text="▶ Начать калибровку", command=self.start_calibration)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="■ Остановить", command=self.stop_calibration, state="disabled")
        self.stop_button.pack(side="left", padx=(7, 0))
        ttk.Button(buttons, text="Экспорт опорных ID…", command=self.export_anchors).pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text="Экспорт диапазонов…", command=self.export_ranges).pack(side="left", padx=(7, 0))
        ttk.Button(buttons, text="Открыть папку результатов", command=self.open_results_folder).pack(side="right")

        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill="x", pady=(0, 8))
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100).pack(fill="x")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w", pady=(3, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        anchors_tab = ttk.Frame(self.notebook, padding=5)
        ranges_tab = ttk.Frame(self.notebook, padding=5)
        log_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(anchors_tab, text="Опорные ID и годы")
        self.notebook.add(ranges_tab, text="Диапазоны по годам")

        self.anchor_tree = self._make_tree(anchors_tab, (
            ("id", "ID пользователя", 145, "e"),
            ("date", "Дата регистрации (МСК)", 190, "center"),
            ("year", "Год", 75, "center"),
            ("status", "Результат", 135, "center"),
            ("detail", "Комментарий", 430, "w"),
        ))
        self._attach_tree_copy_menu(self.anchor_tree, (0,))
        self.range_tree = self._make_tree(ranges_tab, (
            ("year", "Год", 85, "center"),
            ("from", "ID от", 170, "e"),
            ("to", "ID до", 170, "e"),
            ("status", "Способ / статус определения", 500, "w"),
        ))
        self._attach_tree_copy_menu(self.range_tree, (1, 2))

        single_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(single_tab, text="Одиночные ID")
        self.single_tree = self._make_tree(single_tab, (
            ("id", "ID пользователя", 130, "e"),
            ("date", "Дата регистрации (МСК)", 180, "center"),
            ("year", "Год", 65, "center"),
            ("status", "Результат", 110, "center"),
            ("source", "Источник", 110, "center"),
            ("checked", "Проверено", 145, "center"),
            ("detail", "Комментарий", 330, "w"),
        ))
        self._attach_tree_copy_menu(self.single_tree, (0,))
        self.single_tab = single_tab
        self.notebook.add(log_tab, text="Журнал")

        log_controls = ttk.Frame(log_tab)
        log_controls.pack(fill="x", pady=(0, 4))
        ttk.Button(log_controls, text="Копировать весь журнал", command=self.copy_log_all).pack(side="left")
        ttk.Button(log_controls, text="Копировать выделенное", command=self.copy_log_selection).pack(side="left", padx=(6, 0))
        ttk.Button(log_controls, text="Сохранить журнал…", command=self.save_log).pack(side="left", padx=(6, 0))
        ttk.Button(log_controls, text="Очистить журнал", command=self.clear_log).pack(side="right")
        self.log_text = tk.Text(log_tab, wrap="word", height=12, font=("Consolas", 9), state="disabled",
                                undo=False, exportselection=True)
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self._build_log_context_menu()

        note = ttk.Label(root, text=(
            "При checkbox-CAPTCHA откроется отдельное окно Chrome без DevTools: отметьте «Я не робот» вручную. "
            "Для окна проверки требуется Playwright/Chromium. В полях ID работают Ctrl+C/Ctrl+V и меню по правой кнопке; "
            "в таблицах Ctrl+C копирует ID выбранной строки. В журнале: Ctrl+C — выделенное, "
            "Ctrl+Shift+C — всё, Ctrl+A — выделить всё; правый клик открывает меню. "
            "restore.getSettings / restore.init — недокументированные методы VK; диапазоны являются оценкой."
        ), foreground="#8a5a00", wraplength=1100)
        note.pack(fill="x", pady=(8, 0))

    @staticmethod
    def _make_tree(parent: ttk.Frame, columns: tuple) -> ttk.Treeview:
        keys = [column[0] for column in columns]
        tree = ttk.Treeview(parent, columns=keys, show="headings", selectmode="browse")
        for key, heading, width, anchor in columns:
            tree.heading(key, text=heading)
            tree.column(key, width=width, minwidth=55, anchor=anchor, stretch=True)
        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        return tree

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _attach_entry_clipboard(self, entry: ttk.Entry) -> None:
        """Добавляет явные сочетания клавиш и контекстное меню буфера обмена."""
        if not hasattr(self, "entry_context_menu"):
            self.entry_context_menu = tk.Menu(self, tearoff=False)
            self.entry_context_menu.add_command(
                label="Вырезать", command=lambda: self._entry_menu_action("<<Cut>>"))
            self.entry_context_menu.add_command(
                label="Копировать", command=lambda: self._entry_menu_action("<<Copy>>"))
            self.entry_context_menu.add_command(
                label="Вставить", command=lambda: self._entry_menu_action("<<Paste>>"))
            self.entry_context_menu.add_separator()
            self.entry_context_menu.add_command(
                label="Выделить всё", command=self._select_all_entry_text)

        entry.bind("<Button-3>", self._show_entry_context_menu, add="+")
        entry.bind("<Control-c>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Control-C>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Control-v>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Control-V>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Control-x>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")
        entry.bind("<Control-X>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")
        entry.bind("<Control-a>", self._select_all_entry_shortcut, add="+")
        entry.bind("<Control-A>", self._select_all_entry_shortcut, add="+")
        entry.bind("<Control-Insert>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Shift-Insert>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Shift-Delete>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")

    @staticmethod
    def _entry_virtual_event(event: tk.Event, virtual_event: str) -> str:
        try:
            event.widget.event_generate(virtual_event)
        except tk.TclError:
            pass
        return "break"

    def _select_all_entry_shortcut(self, event: tk.Event | None = None) -> str:
        self._select_all_entry_text(event.widget if event is not None else None)
        return "break"

    def _select_all_entry_text(self, entry=None) -> None:
        entry = entry or getattr(self, "_context_entry", None) or self.focus_get()
        if entry is None:
            return
        try:
            entry.selection_range(0, "end")
            entry.icursor("end")
            entry.focus_set()
        except (tk.TclError, AttributeError):
            pass

    def _entry_menu_action(self, virtual_event: str) -> None:
        entry = getattr(self, "_context_entry", None) or self.focus_get()
        if entry is None:
            return
        try:
            entry.focus_set()
            entry.event_generate(virtual_event)
        except (tk.TclError, AttributeError):
            pass

    def _show_entry_context_menu(self, event: tk.Event) -> str:
        self._context_entry = event.widget
        try:
            self.entry_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.entry_context_menu.grab_release()
        return "break"

    def _attach_tree_copy_menu(self, tree: ttk.Treeview, id_columns: tuple[int, ...]) -> None:
        """Включает копирование ID из строки таблицы по Ctrl+C и правому клику."""
        if not hasattr(self, "tree_context_menu"):
            self.tree_context_menu = tk.Menu(self, tearoff=False)
            self.tree_context_menu.add_command(
                label="Копировать ID", command=self._copy_context_tree_ids)
            self.tree_context_menu.add_command(
                label="Копировать всю строку", command=self._copy_context_tree_row)

        tree.bind(
            "<Control-c>",
            lambda event, target=tree, columns=id_columns: self._copy_tree_ids(target, columns),
            add="+",
        )
        tree.bind(
            "<Control-C>",
            lambda event, target=tree, columns=id_columns: self._copy_tree_ids(target, columns),
            add="+",
        )
        tree.bind(
            "<Button-3>",
            lambda event, target=tree, columns=id_columns: self._show_tree_context_menu(
                event, target, columns),
            add="+",
        )

    def _show_tree_context_menu(
        self, event: tk.Event, tree: ttk.Treeview, id_columns: tuple[int, ...]
    ) -> str:
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            tree.focus(item)
        self._context_tree = tree
        self._context_tree_item = item or (tree.selection()[0] if tree.selection() else "")
        self._context_tree_id_columns = id_columns
        try:
            self.tree_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_context_menu.grab_release()
        return "break"

    def _copy_tree_ids(self, tree: ttk.Treeview, id_columns: tuple[int, ...]) -> str:
        selection = tree.selection()
        item = selection[0] if selection else tree.focus()
        if not item:
            return "break"
        values = tree.item(item, "values")
        ids = [
            re.sub(r"[\s,]", "", str(values[index]))
            for index in id_columns
            if index < len(values) and str(values[index]).strip()
        ]
        if ids:
            self.clipboard_clear()
            self.clipboard_append("\t".join(ids))
            self.update_idletasks()
            self.status_var.set("ID скопирован в буфер обмена.")
        return "break"

    def _copy_context_tree_ids(self) -> None:
        tree = getattr(self, "_context_tree", None)
        if tree is not None:
            self._copy_tree_ids(tree, self._context_tree_id_columns)

    def _copy_context_tree_row(self) -> None:
        tree = getattr(self, "_context_tree", None)
        item = getattr(self, "_context_tree_item", "")
        if tree is None or not item:
            return
        values = tree.item(item, "values")
        if values:
            self.clipboard_clear()
            self.clipboard_append("\t".join(str(value) for value in values))
            self.update_idletasks()
            self.status_var.set("Строка таблицы скопирована в буфер обмена.")

    def _build_log_context_menu(self) -> None:
        self.log_context_menu = tk.Menu(self, tearoff=False)
        self.log_context_menu.add_command(label="Копировать выделенное", command=self.copy_log_selection)
        self.log_context_menu.add_command(label="Копировать весь журнал", command=self.copy_log_all)
        self.log_context_menu.add_command(label="Выделить всё", command=self.select_all_log)
        self.log_context_menu.add_separator()
        self.log_context_menu.add_command(label="Сохранить журнал…", command=self.save_log)
        self.log_context_menu.add_command(label="Очистить журнал", command=self.clear_log)
        self.log_text.bind("<Button-3>", self._show_log_context_menu)
        self.log_text.bind("<Control-c>", self._copy_log_shortcut)
        self.log_text.bind("<Control-C>", self._copy_log_shortcut)
        self.log_text.bind("<Control-a>", self._select_all_log_shortcut)
        self.log_text.bind("<Control-A>", self._select_all_log_shortcut)
        self.log_text.bind("<Control-Shift-c>", self._copy_all_log_shortcut)
        self.log_text.bind("<Control-Shift-C>", self._copy_all_log_shortcut)
        self.log_text.bind("<Shift-F10>", self._show_log_context_menu_keyboard)

    def _show_log_context_menu(self, event: tk.Event) -> str:
        try:
            self.log_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_context_menu.grab_release()
        return "break"

    def _show_log_context_menu_keyboard(self, event: tk.Event) -> str:
        try:
            box = self.log_text.bbox("insert") or (8, 8, 0, 0)
            self.log_context_menu.tk_popup(self.log_text.winfo_rootx() + box[0],
                                           self.log_text.winfo_rooty() + box[1])
        finally:
            self.log_context_menu.grab_release()
        return "break"

    def _copy_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.copy_log_selection()
        return "break"

    def _copy_all_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.copy_log_all()
        return "break"

    def _select_all_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.select_all_log()
        return "break"

    def select_all_log(self) -> None:
        self.log_text.tag_remove("sel", "1.0", "end")
        self.log_text.tag_add("sel", "1.0", "end-1c")
        self.log_text.mark_set("insert", "end-1c")
        self.log_text.see("insert")
        self.log_text.focus_set()

    def copy_log_selection(self) -> None:
        try:
            selected = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            self.status_var.set("Нет выделенного текста. Используйте «Копировать весь журнал».")
            return
        self.clipboard_clear()
        self.clipboard_append(selected)
        self.update_idletasks()
        self.status_var.set("Выделенный фрагмент журнала скопирован в буфер обмена.")

    def copy_log_all(self) -> None:
        content = self.log_text.get("1.0", "end-1c")
        if not content:
            self.status_var.set("Журнал пуст.")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update_idletasks()
        self.status_var.set("Весь журнал скопирован в буфер обмена.")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить журнал", defaultextension=".txt",
            initialfile="vk_calibrator_log.txt",
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8-sig")
            self.status_var.set(f"Журнал сохранён: {path}")
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения журнала", str(exc), parent=self)

    def _choose_browser_profile(self) -> None:
        current = self.browser_profile_var.get().strip()
        selected = filedialog.askdirectory(parent=self, title="Выберите папку постоянного профиля Chromium",
                                           initialdir=current if Path(current).is_dir() else str(APP_DIR))
        if selected:
            self.browser_profile_var.set(selected)

    def _choose_browser_executable(self) -> None:
        current = self.browser_executable_var.get().strip()
        selected = filedialog.askopenfilename(
            parent=self, title="Выберите исполняемый файл браузера",
            initialdir=str(Path(current).parent) if current and Path(current).parent.is_dir() else r"C:\Program Files\Google\Chrome\Application",
            filetypes=[("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")]
        )
        if selected:
            self.browser_executable_var.set(selected)

    def _validate_runtime_config(self) -> dict[str, Any] | None:
        """Проверяет общие настройки соединения для калибровки и одиночного запроса."""
        try:
            pause = float(self.pause_var.get().replace(",", "."))
            retries = int(self.retries_var.get())
            api_url = self.api_var.get().strip()
            browser_profile = self.browser_profile_var.get().strip()
            browser_executable = self.browser_executable_var.get().strip()
            if not browser_profile:
                raise ValueError("Укажите папку профиля браузера для CAPTCHA.")
            browser_profile = str(Path(browser_profile).expanduser().resolve())
            if browser_executable:
                browser_executable = str(Path(browser_executable).expanduser().resolve())
                if not Path(browser_executable).is_file():
                    raise ValueError(f"Файл браузера не найден: {browser_executable}")
                if Path(browser_executable).suffix.lower() != ".exe":
                    raise ValueError("Укажите путь именно к исполняемому файлу .exe")
            if pause < 0.2:
                raise ValueError("Пауза не может быть меньше 0,2 секунды.")
            if not 1 <= retries <= 10:
                raise ValueError("Количество повторов должно быть от 1 до 10.")
            if not api_url.startswith(("https://", "http://")):
                raise ValueError("Адрес API должен начинаться с https:// или http://.")
            if not api_url.endswith("/"):
                api_url += "/"
            return {"pause": pause, "retries": retries, "api_url": api_url,
                    "browser_profile": browser_profile,
                    "browser_executable": browser_executable}
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)
            return None

    def _validate_config(self) -> dict[str, Any] | None:
        common = self._validate_runtime_config()
        if common is None:
            return None
        try:
            max_id = int(self.max_id_var.get().replace(",", "").replace(" ", ""))
            points = int(self.points_var.get())
            if max_id < 10:
                raise ValueError("Максимальный ID должен быть не меньше 10.")
            if not 2 <= points <= 5000:
                raise ValueError("Количество опорных точек должно быть от 2 до 5000.")
            return {**common, "max_id": max_id, "points": points,
                    "build_ranges": bool(self.ranges_var.get())}
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)
            return None

    def _get_shared_client(self, config: dict[str, Any]) -> VKRestoreClient:
        """Повторно использует cookie-контекст после CAPTCHA между разными задачами."""
        signature = (config["api_url"], config["browser_profile"], config["browser_executable"])
        if self.shared_client is None or self.shared_client_signature != signature:
            self.shared_client = VKRestoreClient(
                config["api_url"], config["pause"], config["retries"],
                lambda message: self.events.put(("log", message)),
                self.stop_event, config["browser_profile"], config["browser_executable"],
            )
            self.shared_client_signature = signature
        else:
            self.shared_client.pause = max(0.2, config["pause"])
            self.shared_client.retries = max(1, config["retries"])
        return self.shared_client

    def start_calibration(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        config = self._validate_config()
        if config is None:
            return
        self._save_settings()
        # Существующие строки таблиц не очищаем: новые результаты дополняют историю.
        self.progress_var.set(0)
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.single_lookup_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.current_task = "calibration"
        self._log("Запуск калибровки. Кэшированные даты повторно запрашиваться не будут.")
        self._log("При CAPTCHA откроется Chrome без отладочных инструментов. Пройдите проверку вручную; контекст VK будет использоваться последующими запросами.")
        self._log(f"Каталог профиля браузера: {config['browser_profile']}")
        self._log(f"Исполняемый файл браузера: {config['browser_executable'] or 'Chromium Playwright (автовыбор)'}")
        client = self._get_shared_client(config)
        self.worker = CalibrationWorker(config, self.events, self.stop_event, client=client)
        self.worker_thread = threading.Thread(target=self.worker.run, name="VKCalibrator", daemon=True)
        self.worker_thread.start()

    def start_single_lookup(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        raw_uid = self.single_id_var.get().strip().replace(",", "").replace(" ", "")
        try:
            uid = int(raw_uid)
            if uid < 1 or uid > 2_147_483_647:
                raise ValueError
        except ValueError:
            messagebox.showerror("Некорректный VK ID", "Введите целочисленный VK ID от 1 до 2 147 483 647.", parent=self)
            self.single_id_entry.focus_set()
            self.single_id_entry.selection_range(0, "end")
            return

        config = self._validate_runtime_config()
        if config is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.single_lookup_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.current_task = "single"
        self.progress_var.set(0)
        use_cache = bool(self.single_use_cache_var.get())
        self._log(f"Одиночная проверка ID {uid:,}.")
        self._log("Источник: кэш (при наличии)." if use_cache else "Кэш отключён: запрашиваю дату непосредственно у VK.")
        self._log(f"Каталог профиля браузера: {config['browser_profile']}")
        client = self._get_shared_client(config)
        self.worker = SingleProbeWorker(uid, use_cache, client, self.events, self.stop_event)
        self.worker_thread = threading.Thread(target=self.worker.run, name="VKSingleProbe", daemon=True)
        self.worker_thread.start()

    def stop_calibration(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Останавливаю после текущего сетевого запроса…")
            if self.current_task == "single":
                self._log("Получена команда остановки одиночной проверки.")
            else:
                self._log("Получена команда остановки калибровки.")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._log(str(data))
                elif kind == "status":
                    self.status_var.set(str(data))
                elif kind == "progress_init":
                    self.progress_var.set(0)
                elif kind == "progress":
                    current, total, text_value = data
                    self.progress_var.set(current / max(1, total) * 100)
                    self.status_var.set(text_value)
                elif kind == "top":
                    self._log(f"Верхняя граница: ID {data.uid:,}, год {data.year}.")
                elif kind == "anchor":
                    self._insert_anchor(data)
                elif kind == "range":
                    self._insert_range(data)
                elif kind == "single_result":
                    self._insert_single_result(data)
                elif kind == "single_failure":
                    self._log("ОШИБКА одиночного запроса: " + str(data))
                    self.status_var.set("Одиночная проверка завершилась с ошибкой")
                elif kind == "single_done":
                    if data.get("stopped"):
                        self.status_var.set("Одиночная проверка остановлена")
                        self._log("Одиночная проверка остановлена пользователем.")
                    self._finish_worker()
                elif kind == "warning":
                    self._log("ПРЕДУПРЕЖДЕНИЕ: " + str(data))
                    messagebox.showwarning("Проверка монотонности", str(data), parent=self)
                elif kind == "fatal":
                    self._log("ОШИБКА: " + str(data))
                    self.status_var.set("Калибровка завершилась с ошибкой")
                    messagebox.showerror("Ошибка калибровки", str(data), parent=self)
                    self._finish_worker()
                elif kind == "done":
                    if data.get("stopped"):
                        self.status_var.set("Остановлено. Частичные результаты сохранены.")
                        self._log("Калибровка остановлена; частичные результаты сохранены.")
                    else:
                        self.status_var.set("Калибровка завершена")
                        self._log(f"Готово. Опорных точек: {data['anchors']}; диапазонов: {data['ranges']}.")
                    self._finish_worker()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _finish_worker(self) -> None:
        self.start_button.configure(state="normal")
        self.single_lookup_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.worker_thread = None
        self.current_task = None

    @staticmethod
    def _scroll_to_latest(tree: ttk.Treeview, item_id: str) -> None:
        # see() гарантирует видимость конкретной строки; moveto(1.0) дополнительно
        # прижимает список к самому низу, когда записей уже больше высоты окна.
        tree.see(item_id)
        tree.yview_moveto(1.0)

    def _insert_anchor(self, item: ProbeResult) -> None:
        row_key = (item.uid, item.timestamp, item.status, item.detail)
        if row_key in self._anchor_rows_seen:
            return
        self._anchor_rows_seen.add(row_key)
        result_label = "Получена" if item.status == "ok" else ("Недоступна" if item.status == "unavailable" else "Ошибка")
        iid = self.anchor_tree.insert("", "end", values=(
            f"{item.uid:,}", item.date_text,
            item.year if item.year is not None else "—",
            result_label, item.detail or ("Из кэша" if item.from_cache else ""),
        ))
        self._scroll_to_latest(self.anchor_tree, iid)
        self.anchor_count += 1

    def _insert_range(self, item: dict[str, Any]) -> None:
        row_key = (item.get("year"), item.get("id_from"), item.get("id_to"), item.get("status", ""))
        if row_key in self._range_rows_seen:
            return
        self._range_rows_seen.add(row_key)
        iid = self.range_tree.insert("", "end", values=(
            item["year"], f"{item['id_from']:,}", f"{item['id_to']:,}", item["status"]
        ))
        self._scroll_to_latest(self.range_tree, iid)
        self.range_count += 1

    @staticmethod
    def _single_status_label(status: str) -> str:
        return "Получена" if status == "ok" else ("Недоступна" if status == "unavailable" else "Ошибка")

    def _insert_single_history_row(self, item: dict[str, Any]) -> None:
        timestamp = item.get("timestamp")
        try:
            date_text = datetime.fromtimestamp(int(timestamp), MSK).strftime("%d.%m.%Y %H:%M") if timestamp else "—"
        except (TypeError, ValueError, OSError, OverflowError):
            date_text = "—"
        year = item.get("year") or "—"
        source = "Кэш" if item.get("from_cache") else "VK API"
        iid = self.single_tree.insert("", "end", values=(
            f"{int(item.get('id', 0)):,}", date_text, year,
            self._single_status_label(str(item.get("status", "error"))),
            source, item.get("checked_at", ""), item.get("detail", ""),
        ))
        self._scroll_to_latest(self.single_tree, iid)

    def _insert_single_result(self, result: ProbeResult) -> None:
        entry = {
            "id": result.uid,
            "timestamp": result.timestamp,
            "year": result.year,
            "status": result.status,
            "detail": result.detail,
            "from_cache": result.from_cache,
            "checked_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        }
        self.single_history.append(entry)
        self.single_history = self.single_history[-2000:]
        try:
            write_json(SINGLE_RESULTS_FILE, self.single_history)
        except OSError as exc:
            self._log(f"Не удалось сохранить историю одиночных ID: {exc}")
        self._insert_single_history_row(entry)
        self.notebook.select(self.single_tab)
        if result.status == "ok":
            source = " [кэш]" if result.from_cache else ""
            message = f"ID {result.uid:,} → {result.date_text} (год {result.year}){source}"
            self._log(message)
            self.status_var.set(f"Дата регистрации ID {result.uid:,}: {result.date_text}")
        elif result.status == "unavailable":
            message = f"ID {result.uid:,}: дата недоступна ({result.detail})"
            self._log(message)
            self.status_var.set(f"Для ID {result.uid:,} дата недоступна")
        else:
            message = f"ID {result.uid:,}: ошибка — {result.detail}"
            self._log(message)
            self.status_var.set("Одиночная проверка завершилась с ошибкой")

    def _load_single_history(self) -> None:
        data = read_json(SINGLE_RESULTS_FILE, [])
        if not isinstance(data, list):
            return
        self.single_history = [item for item in data if isinstance(item, dict)][-2000:]
        for item in self.single_history:
            try:
                self._insert_single_history_row(item)
            except (TypeError, ValueError, tk.TclError):
                continue
        if self.single_history:
            self._log(f"Загружена история одиночных проверок: {len(self.single_history)} записей.")

    def _load_previous_results(self) -> None:
        data = read_json(RESULTS_FILE, {})
        if not isinstance(data, dict):
            return
        for item in data.get("anchors", []):
            try:
                probe = ProbeResult(int(item["id"]),
                                    int(item["timestamp"]) if item.get("timestamp") is not None else None,
                                    str(item.get("status", "ok")), str(item.get("detail", "")))
                self._insert_anchor(probe)
            except (KeyError, TypeError, ValueError):
                continue
        for item in data.get("ranges", []):
            self._insert_range(item)
        if self.anchor_count or self.range_count:
            self._log(f"Загружены ранее сохранённые результаты от {data.get('saved_at', 'неизвестно')}.")

    def export_anchors(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить опорные ID", defaultextension=".csv",
            initialfile="vk_id_anchors.csv", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        data = read_json(RESULTS_FILE, {})
        rows = data.get("anchors", []) if isinstance(data, dict) else []
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file, delimiter=";")
                writer.writerow(["ID", "Дата регистрации (МСК)", "Год", "Unix timestamp", "Статус", "Комментарий"])
                for item in rows:
                    writer.writerow([item.get("id"), item.get("date"), item.get("year"),
                                     item.get("timestamp"), item.get("status"), item.get("detail")])
            messagebox.showinfo("Экспорт", f"Опорные точки сохранены:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Ошибка записи", str(exc), parent=self)

    def export_ranges(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить диапазоны по годам", defaultextension=".csv",
            initialfile="vk_id_ranges.csv", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        data = read_json(RESULTS_FILE, {})
        rows = data.get("ranges", []) if isinstance(data, dict) else []
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file, delimiter=";")
                writer.writerow(["Год", "ID от", "ID до", "Статус определения"])
                for item in rows:
                    writer.writerow([item.get("year"), item.get("id_from"),
                                     item.get("id_to"), item.get("status")])
            messagebox.showinfo("Экспорт", f"Диапазоны сохранены:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Ошибка записи", str(exc), parent=self)

    def open_results_folder(self) -> None:
        try:
            import os
            os.startfile(str(APP_DIR))
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def _restore_window_state(self) -> None:
        if self._restore_maximized:
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
        self.schedule_settings_save()

    def _on_configure(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._last_normal_geometry = self.geometry()
        except tk.TclError:
            pass
        self.schedule_settings_save()

    def schedule_settings_save(self, *_args) -> None:
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
            self._settings_after_id = self.after(400, self._save_settings)
        except tk.TclError:
            self._settings_after_id = None

    def _save_settings(self) -> None:
        self._settings_after_id = None
        try:
            state = self.state()
            maximized = state in {"zoomed", "maximized"}
            geometry = self.geometry() if state == "normal" else self._last_normal_geometry
            settings = {
                "max_id": self.max_id_var.get(),
                "points": self.points_var.get(),
                "pause": self.pause_var.get(),
                "retries": self.retries_var.get(),
                "api_url": self.api_var.get(),
                "build_ranges": bool(self.ranges_var.get()),
                "browser_profile": self.browser_profile_var.get(),
                "browser_executable": self.browser_executable_var.get(),
                "single_id": self.single_id_var.get(),
                "single_use_cache": bool(self.single_use_cache_var.get()),
                "geometry": str(geometry or "1160x820"),
                "maximized": maximized,
            }
            save_module_settings(
                APP_SETTINGS_FILE,
                "vk_id_calibrator",
                settings,
                window_keys={"geometry", "maximized"},
            )
            self._saved_settings = settings
        except (OSError, tk.TclError, AttributeError, TypeError):
            pass

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Выход", "Калибровка ещё выполняется. Остановить её и закрыть приложение?", parent=self):
                return
            self.stop_event.set()
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
                self._settings_after_id = None
            self._save_settings()
        finally:
            self.destroy()


def main() -> None:
    app = VKCalibratorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
