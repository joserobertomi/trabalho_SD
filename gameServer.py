import sys
import threading
import time
from queue import Queue
import CORBA
import PortableServer
import CosNaming
import TicTacToe
import TicTacToe__POA

SCAVENGER_INTERVAL = 30

class GameFactory_i(TicTacToe__POA.GameFactory):
    def __init__(self, poa):
        self.games = []
        self.iterators = {}
        self.lock = threading.Lock()
        self.poa = poa

        self.iterator_poa = poa.create_POA("IterPOA", None, [])
        self.iterator_poa._get_the_POAManager().activate()

        self.iterator_scavenger = IteratorScavenger(self)

        print("GameFactory_i created.")

    def newGame(self, name):
        try:
            game_poa = self.poa.create_POA("Game-" + name, None, [])

        except PortableServer.POA.AdapterAlreadyExists:
            raise TicTacToe.GameFactory.NameInUse()

        gservant = Game_i(self, name, game_poa)
        gid = game_poa.activate_object(gservant)
        gobj = game_poa.id_to_reference(gid)
        game_poa._get_the_POAManager().activate()

        with self.lock:
            self.games.append((name, gservant, gobj))

        return gobj

    def listGames(self, how_many):
        with self.lock:
            front = self.games[:int(how_many)]
            rest = self.games[int(how_many):]

        ret = list(map(lambda g: TicTacToe.GameInfo(g[0], g[2]), front))

        if rest:
            iter = GameIterator_i(self, self.iterator_poa, rest)
            iid = self.iterator_poa.activate_object(iter)
            iobj = self.iterator_poa.id_to_reference(iid)
            with self.lock:
                self.iterators[iid] = iter
        else:
            iobj = None

        return ret, iobj

    def _removeGame(self, name):
        with self.lock:
            self.games = [game for game in self.games if game[0] != name]

    def _removeIterator(self, iid):
        with self.lock:
            del self.iterators[iid]


class GameIterator_i(TicTacToe__POA.GameIterator):
    def __init__(self, factory, poa, games):
        self.factory = factory
        self.poa = poa
        self.games = games
        self.tick = 1
        print("GameIterator_i created.")

    def __del__(self):
        print("GameIterator_i deleted.")

    def next_n(self, how_many):
        self.tick = 1
        front = self.games[:int(how_many)]
        self.games = self.games[int(how_many):]

        ret = list(map(lambda g: TicTacToe.GameInfo(g[0], g[2]), front))

        more = bool(self.games)
        return ret, more

    def destroy(self):
        id = self.poa.servant_to_id(self)
        self.factory._removeIterator(id)
        self.poa.deactivate_object(id)


class IteratorScavenger(threading.Thread):
    def __init__(self, factory):
        super().__init__()
        self.setDaemon(True)
        self.factory = factory
        self.start()

    def run(self):
        print("Iterator scavenger running...")

        lock = self.factory.lock
        iterators = self.factory.iterators
        poa = self.factory.iterator_poa
        manager = poa._get_the_POAManager()

        while True:
            time.sleep(SCAVENGER_INTERVAL)
            print("Scavenging dead iterators...")

            manager.hold_requests(True)
            with lock:
                for id, iter in list(iterators.items()):
                    if iter.tick == 1:
                        iter.tick = 0
                    else:
                        del iterators[id]
                        poa.deactivate_object(id)

            manager.activate()


class Game_i(TicTacToe__POA.Game):
    def __init__(self, factory, name, poa):
        self.factory = factory
        self.name = name
        self.poa = poa
        self.lock = threading.Lock()

        n = TicTacToe.Nobody
        self.players = 0
        self.state = [[n, n, n], [n, n, n], [n, n, n]]

        self.p_noughts = None
        self.p_crosses = None
        self.whose_go = TicTacToe.Nobody
        self.spectators = []
        self.spectatorNotifier = SpectatorNotifier(self.spectators, self.lock)

        print("Game_i created.")

    def joinGame(self, player):
        with self.lock:
            if self.players == 2:
                raise TicTacToe.Game.CannotJoin()

            if self.players == 0:
                ptype = TicTacToe.Nought
                self.p_noughts = player
            else:
                ptype = TicTacToe.Cross
                self.p_crosses = player
                self.whose_go = TicTacToe.Nought
                self.p_noughts.yourGo(self.state)

            gc = GameController_i(self, ptype)
            id = self.poa.activate_object(gc)
            gobj = self.poa.id_to_reference(id)
            self.players += 1

        return gobj, ptype

    def watchGame(self, spectator):
        self.lock.acquire()
        cookie = len(self.spectators)
        self.spectators.append(spectator)
        self.lock.release()
        return cookie, self.state

    def unwatchGame(self, cookie):
        cookie = int(cookie)
        self.lock.acquire()
        if len(self.spectators) > cookie:
            self.spectators[cookie] = None
        self.lock.release()

    def kill(self):
        self.factory._removeGame(self.name)

        if self.p_noughts:
            try:
                self.p_noughts.gameAborted()
            except CORBA.SystemException as ex:
                print("System exception contacting noughts player")

        if self.p_crosses:
            try:
                self.p_crosses.gameAborted()
            except CORBA.SystemException as ex:
                print("System exception contacting crosses player")

        self.spectatorNotifier.gameAborted()

        self.poa.destroy(1, 0)

        print("Game killed")

    def _play(self, x, y, ptype):
        x = int(x)
        y = int(y)
        """Real implementation of GameController::play()"""
        if self.whose_go != ptype:
            raise TicTacToe.GameController.NotYourGo()

        if x < 0 or x > 2 or y < 0 or y > 2:
            raise TicTacToe.GameController.InvalidCoordinates()

        if self.state[x][y] != TicTacToe.Nobody:
            raise TicTacToe.GameController.SquareOccupied()

        self.state[x][y] = ptype

        w = self._checkForWinner()

        try:
            if w is not None:
                print("Winner:", w)
                self.p_noughts.end(self.state, w)
                self.p_crosses.end(self.state, w)
                self.spectatorNotifier.end(self.state, w)

                # Kill ourselves
                self.factory._removeGame(self.name)
                self.poa.destroy(1, 0)
            else:

                # Tell opponent it's their go
                if ptype == TicTacToe.Nought:
                    self.whose_go = TicTacToe.Cross
                    self.p_crosses.yourGo(self.state)
                else:
                    self.whose_go = TicTacToe.Nought
                    self.p_noughts.yourGo(self.state)

                s = (self.state[0][:], self.state[1][:], self.state[2][:])
                self.spectatorNotifier.queue.put(("update", (s,)))

                # self.spectatorNotifier.up(self.state)

        except (CORBA.COMM_FAILURE, CORBA.OBJECT_NOT_EXIST) as ex:
            print("Lost contact with player!")
            self.kill()

        return self.state

    def _checkForWinner(self):
        """If there is a winner, return the winning player's type. If
        the game is a tie, return Nobody, otherwise return None."""

        # Rows
        for i in range(3):
            if self.state[i][0] == self.state[i][1] and \
                    self.state[i][1] == self.state[i][2] and \
                    self.state[i][0] != TicTacToe.Nobody:
                return self.state[i][0]

        # Columns
        for i in range(3):
            if self.state[0][i] == self.state[1][i] and \
                    self.state[1][i] == self.state[2][i] and \
                    self.state[0][i] != TicTacToe.Nobody:
                return self.state[0][i]

        # Top-left to bottom-right
        if self.state[0][0] == self.state[1][1] and \
                self.state[1][1] == self.state[2][2] and \
                self.state[0][0] != TicTacToe.Nobody:
            return self.state[0][0]

        # Bottom-left to top-right
        if self.state[0][2] == self.state[1][1] and \
                self.state[1][1] == self.state[2][0] and \
                self.state[0][2] != TicTacToe.Nobody:
            return self.state[0][2]

        # Return None if the game is not full
        for i in range(3):
            for j in range(3):
                if self.state[i][j] == TicTacToe.Nobody:
                    return None

        # It's a draw
        return TicTacToe.Nobody

class SpectatorNotifier(threading.Thread):

    # This thread is used to notify all the spectators about changes
    # in the game state. Since there is only one thread, one errant
    # spectator can hold up all the others. A proper event or
    # notification service should make more effort to contact clients
    # concurrently. No matter what happens, the players can't be held
    # up.
    #
    # The implementation uses a simple work queue, which could
    # potentially get backed-up. Ideally, items on the queue should be
    # thrown out if they have been waiting too long.

    def __init__(self, spectators, lock):
        threading.Thread.__init__(self)
        self.setDaemon(1)
        self.spectators = spectators
        self.lock = lock
        self.queue = Queue.Queue(0)
        self.start()

    def apply(func, args, kwargs=None):
        return func(*args) if kwargs is None else func(*args, **kwargs)

    def run(self):
        print("SpectatorNotifier running...")

        while 1:
            method, args = self.queue.get()

            print("Notifying:", method)

            try:
                self.lock.acquire()
                for i in range(len(self.spectators)):
                    spec = self.spectators[i]
                    if spec:
                        try:
                            self.apply(getattr(spec, method), args)
                        except (CORBA.COMM_FAILURE,
                                CORBA.OBJECT_NOT_EXIST) as ex:
                            print("Spectator lost")
                            self.spectators[i] = None
            finally:
                self.lock.release()

    def up(self, state):
        s = (state[0][:], state[1][:], state[2][:])
        self.queue.put(("update", (s,)))

    def end(self, state, winner):
        self.queue.put(("end", (state, winner)))

    def gameAborted(self):
        self.queue.put(("gameAborted", ()))


class GameController_i(TicTacToe__POA.GameController):
    def __init__(self, game, ptype):
        self.game = game
        self.ptype = ptype
        print("GameController_i created.")

    def play(self, x, y):

        return self.game._play(x, y, self.ptype)


class SpectatorNotifier(threading.Thread):
    def __init__(self, spectators, lock):
        super().__init__()
        self.setDaemon(True)
        self.spectators = spectators
        self.lock = lock
        self.queue = Queue(0)
        self.start()

    def run(self):
        print("SpectatorNotifier running...")

        while True:
            method, args = self.queue.get()
            print("Notifying:", method)

            with self.lock:
                for i, spec in enumerate(self.spectators):
                    if spec:
                        try:
                            getattr(spec, method)(*args)
                        except (CORBA.COMM_FAILURE, CORBA.OBJECT_NOT_EXIST):
                            print("Spectator lost")
                            self.spectators[i] = None


def main(argv):
    print("Game Server starting...")

    orb = CORBA.ORB_init(argv, CORBA.ORB_ID)
    poa = orb.resolve_initial_references("RootPOA")
    poa._get_the_POAManager().activate()

    gf_impl = GameFactory_i(poa)
    gf_id = poa.activate_object(gf_impl)
    gf_obj = poa.id_to_reference(gf_id)

    print(orb.object_to_string(gf_obj))

    try:
        nameRoot = orb.string_to_object("IOR:010000002b00000049444c3a6f6d672e6f72672f436f734e616d696e672f4e616d696e67436f6e746578744578743a312e300000010000000000000070000000010102000e0000003139322e3136382e312e31303500f90a0b0000004e616d6553657276696365000300000000000000080000000100000000545441010000001c0000000100000001000100010000000100010509010100010000000901010003545441080000009c9b546701006a14")

        nameRoot = nameRoot._narrow(CosNaming.NamingContext)
        if nameRoot is None:
            print("NameService narrow failed!")
            sys.exit(1)

    except CORBA.ORB.InvalidName:
        print("InvalidName when resolving NameService!")
        sys.exit(1)

    name = [CosNaming.NameComponent("tutorial", "")]
    try:
        tutorialContext = nameRoot.bind_new_context(name)
    except CosNaming.NamingContext.AlreadyBound:
        print('Reusing "tutorial" naming context.')
        tutorialContext = nameRoot.resolve(name)
        tutorialContext = tutorialContext._narrow(CosNaming.NamingContext)
        if tutorialContext is None:
            print('The name "tutorial" is already bound.')
            sys.exit(1)

    tutorialContext.rebind([CosNaming.NameComponent("GameFactory", "")], gf_obj)
    print("GameFactory bound in NameService.")

    orb.run()


if __name__ == "__main__":
    main(sys.argv)